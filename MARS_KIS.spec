# No settings, API keys, cache, test fixtures or reference files are bundled.
a = Analysis(['mars_app.py'], pathex=[], binaries=[], datas=[], hiddenimports=[],
             hookspath=[], hooksconfig={}, runtime_hooks=[], excludes=[], noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name='마스_TOP100',
          debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
          console=False, disable_windowed_traceback=True)
