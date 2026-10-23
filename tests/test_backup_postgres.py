import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('backup_postgres', Path(__file__).resolve().parents[1] / 'tools/backup_postgres.py')
tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tool)


class FullBackupTests(unittest.TestCase):
    def test_complete_export_and_validation_keep_secret_out_of_arguments(self):
        commands = []

        def run(command, env):
            commands.append(command)
            self.assertEqual(env['PGDATABASE'], 'private-test-connection')
            self.assertNotIn('private-test-connection', ' '.join(command))
            if command[0] == 'pg_dump':
                Path(command[-1]).write_bytes(b'archive-test')

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'full.dump'
            with patch.object(tool, 'run_client', side_effect=run):
                digest = tool.backup_database(output, 'private-test-connection', 'pg_dump', 'pg_restore')
            self.assertEqual(output.read_bytes(), b'archive-test')
            self.assertEqual(len(digest), 64)
            self.assertEqual(len(list(Path(directory).iterdir())), 1)
        self.assertEqual(len(commands), 3)
        self.assertIn('--format=custom', commands[0])
        self.assertFalse(any(arg.startswith(('--table', '--schema', '--exclude')) for arg in commands[0]))
        self.assertEqual(commands[1][1], '--list')
        self.assertEqual(commands[2][1:3], ['--file', tool.os.devnull])

    def test_failure_cleans_temporary_and_never_publishes(self):
        for fail_at in (1, 2, 3):
            with self.subTest(fail_at=fail_at), tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / 'full.dump'
                calls = []

                def run(command, env):
                    calls.append(command)
                    if len(calls) == fail_at:
                        raise RuntimeError('simulated failure')
                    if command[0] == 'pg_dump':
                        Path(command[-1]).write_bytes(b'archive-test')

                with patch.object(tool, 'run_client', side_effect=run):
                    with self.assertRaises(RuntimeError):
                        tool.backup_database(output, 'private', 'pg_dump', 'pg_restore')
                self.assertEqual(list(Path(directory).iterdir()), [])

    def test_existing_backup_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'full.dump'
            output.write_bytes(b'original')
            with patch.object(tool, 'run_client') as run:
                with self.assertRaises(RuntimeError):
                    tool.backup_database(output, 'private', 'pg_dump', 'pg_restore')
                run.assert_not_called()
            self.assertEqual(output.read_bytes(), b'original')

    def test_error_does_not_reveal_child_diagnostics(self):
        result = tool.subprocess.CompletedProcess([], 1, stderr=b'private host and password')
        with patch.object(tool.subprocess, 'run', return_value=result):
            with self.assertRaises(RuntimeError) as raised:
                tool.run_client(['pg_dump'], {})
        self.assertNotIn('private host', str(raised.exception))

    def test_publication_race_does_not_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'full.dump'

            def run(command, env):
                if command[0] == 'pg_dump':
                    Path(command[-1]).write_bytes(b'archive-test')
                    output.write_bytes(b'concurrent-backup')

            with patch.object(tool, 'run_client', side_effect=run):
                with self.assertRaises(FileExistsError):
                    tool.backup_database(output, 'private', 'pg_dump', 'pg_restore')
            self.assertEqual(output.read_bytes(), b'concurrent-backup')
            self.assertEqual(len(list(Path(directory).iterdir())), 1)
