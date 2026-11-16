"""Run with python3 -m unittest discover -s tests -p test_pwm_logger.py."""
import csv
import os
import pty
from pathlib import Path
import sys
import tempfile
import threading
import tty
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pwm_logger import parse_sample, record


class PwmLoggerTests(unittest.TestCase):
    def test_validation_and_uart_stream(self):
        self.assertEqual(parse_sample(b'4294967295,-999.00,100,70\r\n'),
                         (4294967295, -999.0, 100, 70))
        for line in (b'header', b'1,nan,0,70', b'1,inf,0,70', b'-1,30,0,70',
                     b'4294967296,30,0,70', b'1,30,101,70', b'1,30,0,-1', b'\xff', b'1,30,0,70,5'):
            self.assertIsNone(parse_sample(line))
        master, slave = pty.openpty()
        tty.setraw(slave)
        errors = []
        with tempfile.TemporaryFile(mode='w+', newline='') as output:
            def run():
                try:
                    record(slave, output, lambda: errors == ['stop'])
                except Exception as exc:
                    errors.append(exc)
            worker = threading.Thread(target=run)
            worker.start()
            try:
                os.write(master, b'time_ms,coolant_temp_c,fan_duty_percent,pump_duty_percent\n123')
                os.write(master, b'45,38.25,60,70\r\nbad\n' + b'x' * 129 + b'1,30,0,70\n0,-999,0,70\n')
                # A marker row lets us wait for the full stream without sleeps.
                os.write(master, b'1,40,60,70\n')
                import time
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    if os.pread(output.fileno(), 4096, 0).count(b'\n') >= 4:
                        break
                    threading.Event().wait(0.01)
                errors.append('stop')
                worker.join(2)
                self.assertFalse(worker.is_alive())
                self.assertEqual(errors, ['stop'])
                output.seek(0)
                rows = list(csv.reader(output))
                self.assertEqual(len(rows), 4)
                self.assertEqual(rows[1][1:], ['12345', '38.25', '60', '70'])
                self.assertEqual(rows[2][1:], ['0', '-999.0', '0', '70'])
                self.assertTrue(rows[1][0].endswith('+00:00'))
            finally:
                if worker.is_alive():
                    errors[:] = ['stop']
                    worker.join(2)
                os.close(master)
                os.close(slave)


if __name__ == '__main__':
    unittest.main()
