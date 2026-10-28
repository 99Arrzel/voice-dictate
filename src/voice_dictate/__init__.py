import sys


def main() -> None:
    if len(sys.argv) == 3 and sys.argv[1] == "transcribe":
        from .daemon import transcribe_file

        transcribe_file(sys.argv[2])
        return
    from .daemon import serve

    serve()
