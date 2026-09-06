import json, os, tempfile, time, unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from host_sandbox.ssh_router import HostConfig, RemoteMCP, SSHRouter, _mac_bytes, _send_magic_packet, wake_host

class RouterTests(unittest.TestCase):
    def test_remote_mcp_over_fake_ssh(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); bindir=root/'bin'; bindir.mkdir()
            ssh=bindir/'ssh'
            repo=Path(__file__).resolve().parents[1]
            ssh.write_text('#!/usr/bin/env bash\nshift 3 2>/dev/null || true\nexec python3 -m host_sandbox.cli --cwd '+repr(str(root))+' stdio\n')
            ssh.chmod(0o755)
            env=os.environ.copy(); env['PATH']=str(bindir)+os.pathsep+env['PATH']; env['PYTHONPATH']=str(repo)
            with patch.dict(os.environ, env, clear=True):
                c=RemoteMCP(HostConfig('test','ignored'))
                try:
                    listed=c.request('tools/list',{})
                    names={x['name'] for x in listed['tools']}
                    self.assertIn('exec_command',names)
                    result=c.request('tools/call',{'name':'exec_command','arguments':{'command':'printf routed','wait_seconds':2}})
                    self.assertFalse(result['isError'])
                    self.assertIn('routed', result['structuredContent']['output_tail'])
                finally:c.close()

    def test_remote_mcp_timeout_resets_stuck_stdio_session(self):
        client = RemoteMCP(HostConfig("stuck", local=True, command="python3 -c 'import time; time.sleep(60)'"))
        started = time.monotonic()
        try:
            with self.assertRaises(TimeoutError):
                client.request("tools/list", {}, timeout_seconds=1)
            self.assertLess(time.monotonic() - started, 3)
            self.assertIsNone(client._proc)
        finally:
            client.close()

    def test_config(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'hosts.json'; p.write_text(json.dumps({'hosts':[{'name':'laptop','ssh':'user@laptop'}]}))
            r=SSHRouter(p)
            try:self.assertIn('laptop',r.hosts)
            finally:r.close()

    def test_wol_config_and_magic_packet(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'hosts.json'
            p.write_text(json.dumps({'hosts':[{'name':'rtx3090','ssh':'jirka@pc','wake_on_lan':{'mac':'01:23:45:67:89:ab','broadcast':'192.168.50.255','port':9,'timeout_seconds':90}}]}))
            r=SSHRouter(p)
            try:
                h=r.hosts['rtx3090']
                self.assertEqual(h.wol_mac, '01:23:45:67:89:ab')
                self.assertEqual(h.wol_broadcast, '192.168.50.255')
                self.assertEqual(h.wake_timeout_seconds, 90)
                fake_sock=MagicMock()
                fake_ctx=MagicMock(); fake_ctx.__enter__.return_value=fake_sock
                with patch('host_sandbox.ssh_router.socket.socket', return_value=fake_ctx):
                    _send_magic_packet(h)
                packet, dest = fake_sock.sendto.call_args.args
                self.assertEqual(dest, ('192.168.50.255', 9))
                self.assertEqual(len(packet), 102)
                self.assertEqual(packet[:6], b'\xff'*6)
                self.assertEqual(packet[6:12], bytes.fromhex('0123456789ab'))
            finally:r.close()

    def test_wake_host_waits_until_ssh_online(self):
        h=HostConfig('rtx3090','jirka@pc',wol_mac='01:23:45:67:89:ab',wake_timeout_seconds=10)
        with patch('host_sandbox.ssh_router._ssh_online', side_effect=[False, False, True]), \
             patch('host_sandbox.ssh_router._send_magic_packet') as send, \
             patch('host_sandbox.ssh_router.time.sleep'):
            result=wake_host(h, 10)
        self.assertTrue(result['online'])
        self.assertTrue(result['woke'])
        send.assert_called_once_with(h)

if __name__=='__main__':unittest.main()
