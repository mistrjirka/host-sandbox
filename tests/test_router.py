import json, os, tempfile, unittest
from pathlib import Path
from unittest.mock import patch

from host_sandbox.ssh_router import HostConfig, RemoteMCP, SSHRouter

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

    def test_config(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'hosts.json'; p.write_text(json.dumps({'hosts':[{'name':'laptop','ssh':'user@laptop'}]}))
            r=SSHRouter(p)
            try:self.assertIn('laptop',r.hosts)
            finally:r.close()

if __name__=='__main__':unittest.main()
