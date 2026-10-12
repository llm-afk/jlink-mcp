"""Fixed-speed contracts and cleanup, with no real probe access."""
import unittest
import importlib
from types import SimpleNamespace
from unittest.mock import Mock, patch

from pydantic import ValidationError

from jlink_mcp import api_models as m
from jlink_mcp.exceptions import ConnectionError
from jlink_mcp.profiles import ProjectProfile

manager_module = importlib.import_module('jlink_mcp.jlink_manager')


class SpeedTests(unittest.TestCase):
    def setUp(self):
        self.manager = object.__new__(manager_module.JLinkManager)
        self.manager._jlink = None
        self.manager._connected = self.manager._target_connected = False
        self.manager._device_serial = self.manager._device_name = None
        self.probe = Mock()
        self.probe.MAX_JTAG_SPEED = 12000
        self.probe.speed_info = SimpleNamespace(BaseFreq=1600000000, MinDiv=32)
        self.probe.speed = 20000
        self.probe.serial_number = 123
        self.probe.target_connected.return_value = True
        self.probe.hardware_status = SimpleNamespace(VTarget=3300)
        self.probe.version = '9.42'
        for p in (patch.object(manager_module, 'find_jlink_dll', return_value=None),
                  patch.object(manager_module.pylink, 'JLink', return_value=self.probe),
                  patch.object(manager_module.device_patch_manager, 'match_device_name', return_value=None),
                  patch.object(manager_module.device_patch_manager, 'find_similar_devices', return_value=[]),
                  patch('jlink_mcp.tools.rtt.reset_rtt_state')):
            p.start()
            self.addCleanup(p.stop)

    def open(self, speed=20000, chip='GD32C103CB'):
        self.manager.connect('123', chip_name=chip, speed_khz=speed)

    def test_fixed_speed_set_before_device_and_forwarded(self):
        self.open()
        self.probe.set_speed.assert_called_once_with(20000)
        self.probe.connect.assert_called_once_with('GD32C103CB', speed=20000)
        names = [call[0] for call in self.probe.mock_calls]
        self.assertLess(names.index('set_speed'), names.index('connect'))
        self.assertEqual(self.probe.MAX_JTAG_SPEED, 50000)
        self.assertEqual(self.manager.get_connection_status().speed_khz, 20000)

    def test_auto_speed_keeps_library_cap(self):
        self.probe.speed = 2000
        self.open('auto')
        self.probe.set_speed.assert_called_once_with(auto=True)
        self.probe.connect.assert_called_once_with('GD32C103CB', speed='auto')
        self.assertEqual(self.probe.MAX_JTAG_SPEED, 12000)

    def test_hardware_cap_rejects_and_releases_probe(self):
        self.probe.speed_info.BaseFreq = 160000000
        with self.assertRaisesRegex(ConnectionError, 'capability'):
            self.open(10000)
        self.probe.connect.assert_not_called()
        self.probe.close.assert_called_once()
        self.assertIsNone(self.manager._jlink)

    def test_unknown_hardware_cap_rejects(self):
        self.probe.speed_info.MinDiv = 0
        with self.assertRaises(ConnectionError):
            self.open()
        self.probe.connect.assert_not_called()

    def test_readback_mismatch_fails_without_fallback(self):
        self.probe.speed = 2000
        with self.assertRaisesRegex(ConnectionError, '20000.*2000'):
            self.open()
        self.probe.connect.assert_called_once_with('GD32C103CB', speed=20000)
        self.probe.close.assert_called_once()
        self.assertFalse(self.manager.is_connected)

    def test_native_connection_failure_keeps_requested_speed(self):
        self.probe.connect.side_effect = OSError('Unspecified error')
        with self.assertRaisesRegex(ConnectionError, '20000'):
            self.open()
        self.probe.connect.assert_called_once_with('GD32C103CB', speed=20000)
        self.probe.close.assert_called_once()

    def test_autodetection_preserves_fixed_speed(self):
        self.open(chip=None)
        self.probe.connect.assert_called_once_with('Cortex-M4', speed=20000)

    def test_api_and_profile_reject_invalid_speed(self):
        for value in (0, 4, 50001, True, 20000.0, '20000', 'adaptive'):
            with self.subTest(value=value):
                with self.assertRaises(ValidationError):
                    m.OpenSession(action='open', chip='test', serial_number='123', speed_khz=value)
                with self.assertRaises(ValidationError):
                    ProjectProfile(name='test', chip='test', speed_khz=value)

    def test_api_accepts_fixed_speed_and_auto(self):
        for value in (5, 20000, 50000, 'auto'):
            self.assertEqual(m.OpenSession(action='open', chip='test', serial_number='123', speed_khz=value).speed_khz, value)


if __name__ == '__main__':
    unittest.main()
