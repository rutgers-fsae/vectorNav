import argparse
from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
import tempfile
import unittest
from aiohttp.test_utils import TestClient, TestServer
from vectornav_dashboard.auth import create_auth_file
from vectornav_dashboard.server import create_app, pwm_status


class PwmDashboardTests(unittest.IsolatedAsyncioTestCase):
    async def test_telemetry_download_snapshot_and_validation(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            coolant = root / 'coolant'
            coolant.mkdir()
            name = 'coolant_20261009T120000_123456Z.csv'
            path = coolant / name
            header = 'received_at_utc,time_ms,coolant_temp_c,fan_duty_percent,pump_duty_percent\n'
            stamp = datetime.now(timezone.utc).isoformat()
            content = header + f'{stamp},12345,38.25,60,70\n'
            path.write_text(content + 'partial')
            (coolant / 'coolant_secret.csv').write_text('secret')
            auth = root / 'auth.json'
            create_auth_file(auth, '123456')
            args = argparse.Namespace(log_dir=root, auth_file=auth,
                control_socket=root/'control.sock', telemetry_socket=root/'telemetry.sock',
                airspeed_socket=root/'airspeed.sock')
            client = TestClient(TestServer(await create_app(args)))
            await client.start_server()
            try:
                response = await client.get('/api/status')
                pwm = (await response.json())['pwm']
                self.assertTrue(pwm['connected'])
                self.assertEqual(pwm['coolant_temp_c'], 38.25)
                self.assertEqual(pwm['fan_duty_percent'], 60)
                response = await client.get('/api/pwm/sessions')
                self.assertEqual([p['filename'] for p in (await response.json())['sessions']], [name])
                response = await client.get('/api/pwm/sessions/' + name)
                self.assertEqual(response.status, 200)
                self.assertEqual(await response.text(), content)
                self.assertIn('attachment', response.headers['Content-Disposition'])
                response = await client.get('/api/pwm/sessions/coolant_secret.csv')
                self.assertEqual(response.status, 404)
                path.unlink()
                path.symlink_to(auth)
                response = await client.get('/api/pwm/sessions/' + name)
                self.assertEqual(response.status, 404)
                path.unlink()
                stale = datetime.now(timezone.utc) - timedelta(seconds=10)
                path.write_text(header + f'{stale.isoformat()},0,-999,0,70\n')
                self.assertFalse(pwm_status(root)['connected'])
                self.assertEqual(pwm_status(root)['coolant_temp_c'], -999)
                path.write_text(header)
                self.assertFalse(pwm_status(root)['connected'])
            finally:
                await client.close()


if __name__ == '__main__':
    unittest.main()
