import importlib.util
import platform
import sys
from ctypes.util import find_library

print("Python:", platform.python_version())
for module in ("numpy", "PIL", "scipy"):
    print(module, "available" if importlib.util.find_spec(module) else "not installed")
if "--require" in sys.argv and any(importlib.util.find_spec(m) is None for m in ("numpy", "PIL")):
    raise SystemExit(1)
if "--require" in sys.argv and platform.system() == "Linux" and any(find_library(m) is None for m in ("SM", "Xi", "Xrender", "Xxf86vm", "Xfixes", "GL", "EGL", "GLESv2")):
    raise SystemExit(1)
