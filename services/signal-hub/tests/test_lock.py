import os
from pathlib import Path
import tempfile
import unittest

from trading_signal_hub.config import ConfigurationError
from trading_signal_hub.telegram import poller_lock


@unittest.skipUnless(os.name == "posix", "Linux poller lock")
class LockTests(unittest.TestCase):
    def test_second_poller_is_denied_and_lock_is_released(self):
        with tempfile.TemporaryDirectory() as directory:
            lock = Path(directory) / "poller.lock"
            with poller_lock(lock):
                with self.assertRaises(ConfigurationError):
                    with poller_lock(lock):
                        self.fail("second owner acquired the lock")
            with poller_lock(lock):
                pass


if __name__ == "__main__":
    unittest.main()
