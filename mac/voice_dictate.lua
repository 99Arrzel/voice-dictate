-- voice-dictate client for Hammerspoon: records with sox, sends the WAV to a voice-dictate server
-- on the network, and pastes the cleaned-up text into the focused app.
local M = {}

local defaults = {
  server = "http://your-pc.local:8765",
  token = "",
  hotkey = { mods = { "ctrl", "alt" }, key = "space" },
  rec = nil,
  maxSeconds = 600,
  sounds = true,
  trailingSpace = true,
}

local LINGER_SECONDS = { done = 1.5, error = 4 }

local config
local state = "idle"
local session = 0
local recorder, startedAt, ticker, stopTimer, lingerTimer
local menubar, hotkey, cancelKey
local statusLine, serverLine, lastResult = "Starting…", "", ""

local function tmpPath(id)
  local dir = os.getenv("TMPDIR") or "/tmp/"
  if dir:sub(-1) ~= "/" then dir = dir .. "/" end
  return dir .. "voice-dictate-" .. id .. ".wav"
end

local function findRec()
  for _, path in ipairs({ "/opt/homebrew/bin/rec", "/usr/local/bin/rec" }) do
    if hs.fs.attributes(path) then return path end
  end
end

local function sound(name)
  if not config.sounds then return end
  local clip = hs.sound.getByName(name)
  if clip then clip:play() end
end

local function headers()
  return { Authorization = "Bearer " .. config.token, ["Content-Type"] = "audio/wav" }
end

local function show(title, line)
  statusLine = line
  menubar:setTitle(title)
end

local function showIdle()
  if serverLine:match("^Server OK") then
    show("🎙", "Ready · " .. table.concat(config.hotkey.mods, "+") .. "+" .. config.hotkey.key .. " to dictate")
  else
    show("🎙 offline", "Server not reachable")
  end
end

local function finish(kind, title, result)
  state = "idle"
  lastResult = "Last (" .. os.date("%H:%M") .. "): " .. result
  show(title, result)
  if lingerTimer then lingerTimer:stop() end
  lingerTimer = hs.timer.doAfter(LINGER_SECONDS[kind], function()
    if state == "idle" then showIdle() end
  end)
end

local function stopTimers()
  if ticker then ticker:stop(); ticker = nil end
  if stopTimer then stopTimer:stop(); stopTimer = nil end
  cancelKey:disable()
end

local function checkServer()
  hs.http.asyncGet(config.server .. "/health", headers(), function(status, body)
    if status == 200 then
      serverLine = "Server OK · " .. config.server
    elseif status == 401 then
      serverLine = "Server rejected the token · " .. config.server
    elseif status < 0 then
      serverLine = "Server unreachable · " .. config.server
    else
      serverLine = "Server error " .. status .. " · " .. config.server
    end
    if state == "idle" then showIdle() end
  end)
end

local function deliver(status, body)
  if status < 0 then
    serverLine = "Server unreachable · " .. config.server
    sound("Basso")
    return finish("error", "⚠️ offline", "server unreachable: " .. config.server)
  end
  local ok, payload = pcall(hs.json.decode, body or "")
  if not ok or type(payload) ~= "table" then payload = {} end
  if status == 401 then
    sound("Basso")
    return finish("error", "⚠️ token", "server rejected the token")
  end
  if status ~= 200 then
    sound("Basso")
    return finish("error", "⚠️ failed", "server error " .. status .. ": " .. tostring(payload.error or body))
  end

  local text = payload.text or ""
  if text == "" then
    local reason = payload.reason or "nothing heard"
    return finish("error", "⚠️ " .. reason, reason)
  end
  if config.trailingSpace and not text:match("\n$") then text = text .. " " end
  -- Pasting keeps Unicode and line breaks intact; typed Returns would submit chat inputs.
  hs.pasteboard.setContents(text)
  hs.eventtap.keyStroke({ "cmd" }, "v", 0)
  sound("Glass")
  local _, words = text:gsub("%S+", "")
  finish("done", "✅", "typed " .. words .. " words · also on the clipboard")
end

local function upload(id, path)
  local file = io.open(path, "rb")
  if not file then
    sound("Basso")
    return finish("error", "⚠️ failed", "no recording was written")
  end
  local body = file:read("a")
  file:close()
  os.remove(path)
  hs.http.asyncPost(config.server .. "/transcribe", body, headers(), function(status, response)
    if id == session then deliver(status, response) end
  end)
end

local function onRecorderExit(id, path, exitCode, stdErr)
  if id ~= session then
    os.remove(path)
    return
  end
  if state == "recording" then
    -- sox quit on its own: no input device, or no microphone permission for Hammerspoon.
    stopTimers()
    sound("Basso")
    return finish("error", "⚠️ mic", "recorder failed (" .. exitCode .. "): " .. (stdErr or ""):sub(1, 120))
  end
  if state == "processing" then upload(id, path) end
end

local function start()
  local rec = config.rec or findRec()
  if not rec then
    sound("Basso")
    return finish("error", "⚠️ sox", "sox not found: brew install sox")
  end
  session = session + 1
  local id, path = session, tmpPath(session)
  recorder = hs.task.new(rec, function(exitCode, _, stdErr)
    onRecorderExit(id, path, exitCode, stdErr)
  end, { "-q", "-c", "1", "-r", "16000", "-b", "16", "-e", "signed-integer", path })
  if not recorder:start() then
    sound("Basso")
    return finish("error", "⚠️ mic", "could not start " .. rec)
  end
  state = "recording"
  startedAt = hs.timer.secondsSinceEpoch()
  if lingerTimer then lingerTimer:stop() end
  cancelKey:enable()
  sound("Tink")
  show("🔴 0:00", "Recording · press the shortcut again to stop, Esc to cancel")
  ticker = hs.timer.doEvery(1, function()
    local elapsed = math.floor(hs.timer.secondsSinceEpoch() - startedAt)
    menubar:setTitle(string.format("🔴 %d:%02d", elapsed // 60, elapsed % 60))
  end)
  stopTimer = hs.timer.doAfter(config.maxSeconds, function() M.toggle() end)
end

local function stop()
  stopTimers()
  local duration = hs.timer.secondsSinceEpoch() - startedAt
  if duration < 0.4 then
    session = session + 1
    recorder:interrupt()
    return finish("error", "⚠️ too short", "too short, ignored")
  end
  state = "processing"
  sound("Pop")
  show("⏳", "Transcribing on " .. config.server)
  -- SIGINT lets sox finalize the WAV; the upload starts from its exit callback.
  recorder:interrupt()
end

function M.cancel()
  if state ~= "recording" then return end
  stopTimers()
  session = session + 1
  recorder:interrupt()
  sound("Funk")
  finish("error", "⚠️ cancelled", "cancelled")
end

function M.toggle()
  if state == "idle" then
    start()
  elseif state == "recording" then
    stop()
  end
end

function M.start(options)
  config = {}
  for key, value in pairs(defaults) do config[key] = value end
  for key, value in pairs(options or {}) do config[key] = value end
  config.server = config.server:gsub("/+$", "")
  if config.token == "" then
    hs.alert.show("voice-dictate: set token in ~/.hammerspoon/init.lua")
  end

  menubar = hs.menubar.new()
  menubar:setMenu(function()
    checkServer()
    local items = { { title = statusLine, disabled = true } }
    if serverLine ~= "" then table.insert(items, { title = serverLine, disabled = true }) end
    if lastResult ~= "" then table.insert(items, { title = lastResult, disabled = true }) end
    table.insert(items, { title = "-" })
    table.insert(items, { title = "Check server", fn = checkServer })
    return items
  end)
  hotkey = hs.hotkey.bind(config.hotkey.mods, config.hotkey.key, M.toggle)
  cancelKey = hs.hotkey.new({}, "escape", M.cancel)
  show("🎙", "Checking server…")
  checkServer()
  return M
end

return M
