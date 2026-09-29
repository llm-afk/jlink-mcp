import io
import unittest
from unittest.mock import Mock, patch

from jlink_mcp import target_access, gdb_server
from jlink_mcp.jlink_manager import jlink_manager
from jlink_mcp.tools import memory, flash, debug, rtt, svd


class Contracts(unittest.TestCase):
    def setUp(self):
        self.probe = Mock()
        self.probe.halted.return_value = True
        self.access = patch.object(jlink_manager, 'get_jlink', return_value=self.probe)
        self.access.start()
        rtt.reset_rtt_state()

    def tearDown(self):
        rtt.reset_rtt_state()
        self.access.stop()

    def test_read_width_and_byte_order(self):
        for width, values in [(8, [0x12, 0x34, 0x56, 0x78]), (16, [0x3412, 0x7856]), (32, [0x78563412])]:
            self.probe.memory_read.return_value = values
            result = memory.read_memory(0x20000000, 4, width)
            self.assertEqual(result['data'], [0x12, 0x34, 0x56, 0x78])
            self.probe.memory_read.assert_called_with(0x20000000, 32 // width, nbits=width)
        self.probe.halt.assert_not_called()

    def test_write_width_and_byte_order(self):
        self.probe.memory_write.return_value = 4
        self.assertTrue(memory.write_memory(0x20000000, '12 34 56 78', 32)['success'])
        self.probe.memory_write.assert_called_once_with(0x20000000, [0x78563412], nbits=32)

    def test_invalid_memory_inputs_never_access_probe(self):
        for address, size, width in [(0, 3, 32), (1, 4, 32), (0xFFFFFFFF, 4, 8), (0, 4, 24)]:
            self.assertFalse(memory.read_memory(address, size, width)['success'])
        self.assertFalse(memory.write_memory(0, '01 02 03', 24)['success'])
        self.probe.memory_read.assert_not_called()
        self.probe.memory_write.assert_not_called()

    def test_short_read_and_write_fail(self):
        self.probe.memory_read.return_value = []
        self.assertFalse(memory.read_memory(0, 4)['success'])
        self.probe.memory_write.return_value = 2
        self.assertFalse(memory.write_memory(0, '01 02 03 04')['success'])

    def test_svd_address_width(self):
        self.probe.memory_read.return_value = [0x12345678]
        self.assertEqual(svd.read_register_by_address(0x20000000)['raw_value'], 0x12345678)
        self.probe.memory_read.assert_called_with(0x20000000, 1, nbits=32)

    def test_failed_halt_never_reads_registers_or_resets(self):
        self.probe.halted.return_value = False
        with patch.object(target_access.time, 'monotonic', side_effect=[0, 2]):
            result = memory.read_registers(['PC'])
        self.assertFalse(result['success'])
        self.probe.register_read.assert_not_called()
        self.probe.reset.assert_not_called()

    def test_register_aliases_and_partial_errors(self):
        self.probe.register_read.side_effect = [0x08000100, ValueError('bad register')]
        result = memory.read_registers(['PC', 'BAD'])
        self.assertFalse(result['success'])
        self.assertEqual(len(result['registers']), 1)
        self.assertEqual(result['errors'][0]['name'], 'BAD')
        self.assertEqual(self.probe.register_read.call_args_list[0].args, ('R15 (PC)',))

    def test_running_during_register_snapshot_discards_data(self):
        self.probe.halted.side_effect = [True, True, False]
        self.probe.register_read.return_value = 0
        result = memory.read_registers(['PC'])
        self.assertFalse(result['success'])
        self.assertEqual(result['registers'], [])

    def test_range_erase_never_calls_driver(self):
        for args in [dict(start_address=0x08000000, end_address=0x08000400),
                     dict(chip_erase=True, start_address=0x08000000), {}]:
            self.assertFalse(flash.erase_flash(**args)['success'])
        self.probe.erase.assert_not_called()

    def test_explicit_chip_erase(self):
        self.assertTrue(flash.erase_flash(chip_erase=True)['success'])
        self.probe.erase.assert_called_once_with()

    def test_program_verify_failure(self):
        self.probe.memory_read.return_value = [0]
        result = flash.program_flash(0x08000000, '01 02')
        self.assertFalse(result['success'])
        self.assertEqual(result['verify_result']['mismatch_count'], 2)

    def test_verify_short_read_counts_missing_bytes(self):
        result = flash._build_verify_result(b'\x01\x02', b'\x01', 0)
        self.assertEqual(result['mismatch_count'], 1)
        self.assertIsNone(result['mismatches'][0]['actual'])

    def test_sector_short_read_is_not_erased(self):
        self.probe.memory_read.return_value = []
        self.assertFalse(flash.erase_sector(0x08000000)['success'])

    def test_program_rejects_containers_and_ambiguous_input(self):
        self.assertFalse(flash.program_flash(0, file_path='image.elf')['success'])
        self.assertFalse(flash.program_flash(0, data='01', file_path='image.bin')['success'])
        self.probe.flash.assert_not_called()

    def test_invalid_reset_never_resets(self):
        self.assertFalse(debug.reset_target('typo')['success'])
        self.probe.reset.assert_not_called()

    def test_core_reset_restores_strategy_on_failure(self):
        self.probe.set_reset_strategy.return_value = 0
        self.probe.reset.side_effect = RuntimeError('failed reset')
        self.assertFalse(debug.reset_target('core')['success'])
        self.assertEqual(self.probe.set_reset_strategy.call_args.args, (0,))

    def test_rtt_invalid_inputs_do_not_start_driver(self):
        for kwargs in ({'timeout_ms': -1}, {'buffer_index': -1}, {'read_mode': 'bad'}, {'block_address': 1}):
            self.assertFalse(rtt.rtt_start(**kwargs)['success'])
        self.probe.rtt_start.assert_not_called()

    def start_rtt(self, **kwargs):
        self.assertTrue(rtt.rtt_start(**kwargs)['success'])

    def test_rtt_idempotent_and_configured_channel(self):
        self.start_rtt(buffer_index=2, block_address=0x20000010)
        self.start_rtt(buffer_index=2, block_address=0x20000010)
        self.probe.rtt_start.assert_called_once_with(0x20000010)
        self.probe.rtt_read.return_value = [65]
        self.assertEqual(rtt.rtt_read()['data'], 'A')
        self.probe.rtt_read.assert_called_with(2, 1024)

    def test_rtt_split_utf8_and_binary(self):
        self.start_rtt(timeout_ms=0)
        self.probe.rtt_read.side_effect = [[0xE4], [0xB8, 0xAD], [0xFF]]
        self.assertEqual(rtt.rtt_read()['pending_utf8_bytes'], 1)
        self.assertEqual(rtt.rtt_read()['data'], '中')
        self.assertEqual(rtt.rtt_read()['data_hex'], 'ff')

    def test_rtt_waits_for_data(self):
        self.start_rtt(timeout_ms=100)
        self.probe.rtt_read.side_effect = [[], [], [65]]
        self.assertEqual(rtt.rtt_read()['data'], 'A')
        self.assertEqual(self.probe.rtt_read.call_count, 3)

    def test_rtt_timeout_and_once(self):
        self.start_rtt(timeout_ms=0)
        self.probe.rtt_read.return_value = []
        self.assertFalse(rtt.rtt_read()['timed_out'])
        self.assertTrue(rtt.rtt_read(timeout_ms=1)['timed_out'])
        self.start_rtt(read_mode='once', timeout_ms=100)
        self.probe.rtt_read.reset_mock()
        self.assertFalse(rtt.rtt_read()['timed_out'])
        self.probe.rtt_read.assert_called_once()

    def test_rtt_partial_write_sends_suffix_only(self):
        self.start_rtt()
        self.probe.rtt_write.side_effect = [2, 0, 3]
        result = rtt.rtt_write('ABCDE')
        self.assertTrue(result['success'])
        self.assertEqual([c.args[1] for c in self.probe.rtt_write.call_args_list], [b'ABCDE', b'CDE', b'CDE'])

    def test_rtt_partial_timeout_reports_progress(self):
        self.start_rtt(timeout_ms=0)
        self.probe.rtt_write.return_value = 2
        result = rtt.rtt_write('ABCDE')
        self.assertFalse(result['success'])
        self.assertEqual(result['bytes_written'], 2)

    def test_rtt_reset_disconnect_and_new_session(self):
        self.start_rtt()
        debug.reset_target('normal')
        self.assertFalse(rtt.rtt_get_status()['started'])
        self.start_rtt()
        with patch.object(jlink_manager, '_jlink', self.probe):
            with patch.object(type(jlink_manager), 'is_connected', property(lambda self: False)):
                jlink_manager.disconnect()
        self.assertFalse(rtt.rtt_get_status()['started'])
        self.start_rtt()
        with patch.object(jlink_manager, 'get_jlink', return_value=Mock()):
            self.assertFalse(rtt.rtt_get_status()['started'])


class GDB(unittest.TestCase):
    def test_failed_start_is_not_reported_ready(self):
        manager = gdb_server.GDBServerManager()
        process = Mock(stdout=io.BytesIO(b'ERROR: device busy\n'), stderr=io.BytesIO())
        process.poll.return_value = 1
        with patch.object(type(jlink_manager), 'is_connected', property(lambda self: True)), \
             patch.object(jlink_manager, '_device_name', 'GD32C103CB'), \
             patch.object(manager, '_find_jlink_gdbserver_exe', return_value='GDB.exe'), \
             patch.object(gdb_server.subprocess, 'Popen', return_value=process):
            with self.assertRaises(Exception):
                manager.start()
            self.assertFalse(manager.is_running)
            self.assertTrue(process.stdout.closed)

    def test_ready_command_defaults_and_cleanup(self):
        manager = gdb_server.GDBServerManager()
        process = Mock(stdout=io.BytesIO(b'Listening on TCP/IP port 2331\n'), stderr=io.BytesIO())
        process.poll.return_value = None
        with patch.object(type(jlink_manager), 'is_connected', property(lambda self: True)), \
             patch.object(jlink_manager, '_device_name', 'GD32C103CB'), \
             patch.object(manager, '_find_jlink_gdbserver_exe', return_value='GDB.exe'), \
             patch.object(gdb_server.subprocess, 'Popen', return_value=process) as popen:
            try:
                manager.start()
                args = popen.call_args.args[0]
                self.assertIn('GD32C103CB', args)
                self.assertEqual(args[args.index('-LocalhostOnly')+1], '1')
                self.assertIn('-nohalt', args)
                self.assertTrue(manager.is_running)
            finally:
                manager.stop()
            self.assertTrue(process.stdout.closed)


if __name__ == '__main__':
    unittest.main()
