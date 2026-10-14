"""نقطة تشغيل البرنامج. ‎python main.py‎ يفتح الواجهة، و ‎python main.py --smoke‎ يختبرها دون شاشة."""
import sys

from syncfile.ui_arabic import run, smoke_test

if __name__ == "__main__":
    sys.exit(smoke_test() if "--smoke" in sys.argv else run())
