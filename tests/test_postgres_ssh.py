import argparse
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

from tools import postgres_ssh as tunnel
from psycopg2.extensions import parse_dsn


class PostgresSSHTests(unittest.TestCase):
    def arguments(self, *flags):
        parser = argparse.ArgumentParser()
        with patch.dict('os.environ', {}, clear=True):
            tunnel.add_ssh_arguments(parser)
        return parser.parse_args(flags)

    def test_disabled_never_spawns_or_touches_key(self):
        with patch.object(tunnel.subprocess, 'Popen') as spawn:
            with tunnel.database_connection('unchanged', self.arguments()) as url:
                self.assertEqual(url, 'unchanged')
            spawn.assert_not_called()

    def test_loopback_key_path_only_and_tls_credentials_preserved(self):
        args = self.arguments('--ssh-host', 'example-alias', '--ssh-key', 'fixture-key.pem')
        process = MagicMock()
        process.poll.return_value = None
        reservation = MagicMock()
        reservation.getsockname.return_value = ('127.0.0.1', 15432)
        reservation.__enter__.return_value = reservation
        with patch.object(tunnel.shutil, 'which', return_value='ssh'), patch.object(
            Path, 'is_file', return_value=True
        ), patch.object(Path, 'read_bytes', side_effect=AssertionError('must not read key')), patch.object(
            Path, 'read_text', side_effect=AssertionError('must not read key')
        ), patch.object(tunnel.socket, 'socket', return_value=reservation), patch.object(
            tunnel.socket, 'create_connection', return_value=MagicMock()
        ), patch.object(tunnel.subprocess, 'Popen', return_value=process) as spawn:
            with tunnel.database_connection('host=db.example.invalid port=5432 dbname=fixture user=demo password=private-fixture sslmode=verify-full', args) as url:
                params = parse_dsn(url)
                self.assertEqual(params['hostaddr'], '127.0.0.1')
                self.assertEqual(params['port'], '15432')
                self.assertEqual(params['host'], 'db.example.invalid')
                self.assertEqual(params['password'], 'private-fixture')
                self.assertEqual(params['sslmode'], 'verify-full')
            command = spawn.call_args.args[0]
            self.assertNotIn('private-fixture', ' '.join(command))
            self.assertIn('fixture-key.pem', command)
            self.assertIn('StrictHostKeyChecking=yes', command)
            self.assertIn('127.0.0.1:15432:127.0.0.1:5432', command)
            process.terminate.assert_called_once()
            process.wait.assert_called_once_with(timeout=5)

    def test_early_exit_is_reported_without_stderr(self):
        process = MagicMock()
        process.poll.return_value = 255
        reservation = MagicMock()
        reservation.__enter__.return_value = reservation
        reservation.getsockname.return_value = ('127.0.0.1', 15432)
        with patch.object(tunnel.shutil, 'which', return_value='ssh'), patch.object(
            tunnel.socket, 'socket', return_value=reservation
        ), patch.object(tunnel.subprocess, 'Popen', return_value=process):
            with self.assertRaisesRegex(ValueError, 'SSH tunnel failed'):
                with tunnel.database_connection('dbname=fixture', self.arguments('--ssh-host', 'example')):
                    self.fail('Must not reach export')

    def test_export_error_always_closes_tunnel(self):
        process = MagicMock()
        process.poll.return_value = None
        reservation = MagicMock()
        reservation.__enter__.return_value = reservation
        reservation.getsockname.return_value = ('127.0.0.1', 15432)
        with patch.object(tunnel.shutil, 'which', return_value='ssh'), patch.object(
            tunnel.socket, 'socket', return_value=reservation
        ), patch.object(tunnel.socket, 'create_connection', return_value=MagicMock()), patch.object(
            tunnel.subprocess, 'Popen', return_value=process
        ):
            with self.assertRaises(RuntimeError):
                with tunnel.database_connection('dbname=fixture', self.arguments('--ssh-host', 'example')):
                    raise RuntimeError('export failed')
            process.terminate.assert_called_once()
