"""Offline contract tests. Never contacts an exchange or a live relay."""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT/'test_vendor'))
temp=tempfile.TemporaryDirectory(dir=ROOT)
os.environ['WEBHOOK_SECRET']='offline-test-key-only'
os.environ['RELAY_DB_PATH']=str(Path(temp.name)/'queue.sqlite3')
os.environ['ENABLE_LEGACY_BITGET']='false'
spec=importlib.util.spec_from_file_location('relay',ROOT/'bitget_bot.py')
relay=importlib.util.module_from_spec(spec)
spec.loader.exec_module(relay)

class RelayTests(unittest.TestCase):
    def setUp(self):
        with relay.db_connect() as db: db.execute('DELETE FROM signals')
        self.client=relay.app.test_client()
        self.headers={'Authorization':'Bearer offline-test-key-only'}
        self.signal={'secret':'offline-test-key-only','symbol':'BTCUSD','action':'long'}

    def put(self,**changes):
        return self.client.post('/mt5order',json={**self.signal,**changes})

    def poll(self,symbol='BTCUSD',strategy=''):
        return self.client.get('/mt5poll',query_string={'symbol':symbol,'strategy':strategy},headers=self.headers)

    def test_legacy_four_symbols_and_actions(self):
        for symbol in ('BTCUSD','XAUUSD','NAS100.r','SP500ft.r'):
            for action in ('long','short','close_long','close_short'):
                self.assertEqual(self.put(symbol=symbol,action=action).status_code,200)
                r=self.client.get('/mt5poll',query_string={'secret':os.environ['WEBHOOK_SECRET'],'symbol':symbol})
                self.assertEqual(r.json['status'],'order')
                self.assertEqual(r.json['order']['action'],action)
                self.assertEqual(r.json['order']['symbol'],symbol)
                self.assertEqual(self.poll(symbol).json['status'],'empty')

    def test_auth_and_malformed_no_queue(self):
        self.assertEqual(self.put(secret='wrong').status_code,403)
        for value in ([],None,7,{},'text'):
            self.assertIn(self.client.post('/mt5order',json=value).status_code,(400,403))
        for changes in ({'lots':-1},{'lots':'NaN'},{'lots':True},{'symbol':'UNKNOWN'},{'action':'close_all'},{'id':42},{'strategy':'../x'}):
            self.assertEqual(self.put(**changes).status_code,400)
        self.assertEqual(self.poll().json['status'],'empty')
        self.assertEqual(self.client.get('/status').status_code,403)
        self.assertEqual(self.client.post('/mt5order',data=b'x'*20000).status_code,413)

    def test_explicit_id_dedup_and_strategy_scope(self):
        self.assertEqual(self.put(id='event-1').json['status'],'OK')
        self.assertEqual(self.put(id='event-1').json['status'],'duplicate')
        self.assertEqual(self.put(id='event-1',strategy='other').json['status'],'OK')
        self.assertEqual(self.poll().json['status'],'order')
        self.assertEqual(self.poll().json['status'],'empty')
        self.assertEqual(self.put(id='event-1').json['status'],'duplicate')
        self.assertEqual(self.poll(strategy='other').json['status'],'order')

    def test_no_id_repeated_signals_remain_distinct(self):
        self.put(); self.put()
        self.assertNotEqual(self.poll().json['order']['id'],self.poll().json['order']['id'])

    def test_full_queue_rejects_not_drops_oldest(self):
        for i in range(100): self.assertEqual(self.put(id=f'e{i}').status_code,200)
        self.assertEqual(self.put(id='overflow').status_code,429)
        r=self.poll().json['order']
        self.assertEqual(r['id'],relay.hashlib.sha256(relay.json.dumps(['BTCUSD','','e0']).encode()).hexdigest())

    def test_durable_db_across_module_reload(self):
        self.put()
        other_spec=importlib.util.spec_from_file_location('other',ROOT/'bitget_bot.py')
        other=importlib.util.module_from_spec(other_spec); other_spec.loader.exec_module(other)
        self.assertEqual(other.app.test_client().get('/mt5poll?symbol=BTCUSD',headers=self.headers).json['status'],'order')

    def test_expiry_opt_in(self):
        self.put()
        with relay.db_connect() as db: db.execute('UPDATE signals SET created=0')
        with patch.object(relay,'SIGNAL_TTL_SECONDS',300):
            self.assertEqual(self.poll().json['status'],'empty')

    def test_legacy_exchange_is_inactive(self):
        with patch.object(relay.requests,'post',side_effect=AssertionError('network not allowed')):
            self.assertEqual(self.client.post('/webhook',json=self.signal).status_code,410)
            self.assertEqual(self.client.get('/price/BTCUSDT').status_code,410)

    def test_health_excludes_secrets(self):
        r=self.client.get('/healthz')
        self.assertEqual(r.status_code,200)
        self.assertEqual(r.json['version'],'3.4.0')
        self.assertNotIn(os.environ['WEBHOOK_SECRET'],r.get_data(as_text=True))

    def test_missing_secret_refuses_start(self):
        env={**os.environ,'PYTHONPATH':os.pathsep.join(sys.path)}; env.pop('WEBHOOK_SECRET')
        r=subprocess.run([sys.executable,'-X','utf8','-c','import bitget_bot'],cwd=ROOT,env=env,capture_output=True,text=True,encoding='utf-8',errors='replace')
        self.assertNotEqual(r.returncode,0)
        self.assertIn('WEBHOOK_SECRET must be configured',r.stderr)

if __name__=='__main__':
    unittest.main(verbosity=2)
