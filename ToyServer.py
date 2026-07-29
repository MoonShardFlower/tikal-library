"""Entry Point for the Nuitka compilation"""

# nuitka-project: --msvc=latest
# nuitka-project: --mode=standalone
# nuitka-project: --include-windows-runtime-dlls=yes
# nuitka-project: --windows-console-mode=disable
# nuitka-project: --show-modules
# nuitka-project: --report=build-report.xml

# nuitka-project: --include-package=bleak
# nuitka-project: --include-package=bleak.backends.winrt

# nuitka-project: --include-package=winrt._winrt
# nuitka-project: --include-package=winrt._winrt_windows_devices_bluetooth
# nuitka-project: --include-package=winrt._winrt_windows_devices_bluetooth_advertisement
# nuitka-project: --include-package=winrt._winrt_windows_devices_bluetooth_genericattributeprofile
# nuitka-project: --include-package=winrt._winrt_windows_devices_enumeration
# nuitka-project: --include-package=winrt._winrt_windows_devices_radios
# nuitka-project: --include-package=winrt._winrt_windows_foundation
# nuitka-project: --include-package=winrt._winrt_windows_foundation_collections
# nuitka-project: --include-package=winrt._winrt_windows_storage_streams
# nuitka-project: --include-package=winrt.runtime
# nuitka-project: --include-package=winrt.runtime._internals
# nuitka-project: --include-package=winrt.runtime.interop
# nuitka-project: --include-package=winrt.system
# nuitka-project: --include-package=winrt.system.hresult

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
