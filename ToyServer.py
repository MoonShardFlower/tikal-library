"""Entry Point for the Nuitka compilation"""

# nuitka-project: --msvc=latest
# nuitka-project: --mode=standalone
# nuitka-project: --include-windows-runtime-dlls=yes
# nuitka-project: --windows-console-mode=disable
# nuitka-project: --show-modules
# nuitka-project: --report=build-report.xml

# nuitka-project: --include-package=bleak
# nuitka-project: --include-package=bleak.backends.winrt

# nuitka-project: --include-package=winrt
# PyWinRT 4 stores namespace projections in _table.pywinrt data files.
# nuitka-project: --include-package-data=winrt

# nuitka-project: --nofollow-import-to=mypy
# nuitka-project: --nofollow-import-to=rich
# nuitka-project: --nofollow-import-to=pygments
# nuitka-project: --nofollow-import-to=pydantic.v1
# nuitka-project: --nofollow-import-to=pydantic.mypy
# nuitka-project: --nofollow-import-to=pydantic.deprecated
# nuitka-project: --nofollow-import-to=bleak.backends.p4android

from tikal.websocket.cli import main

if __name__ == "__main__":
    main()
