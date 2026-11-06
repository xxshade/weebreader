"""Disposable deliberate-defect controls for the production regression tests."""
import json,os,subprocess,sys,tempfile
from pathlib import Path
root=Path(__file__).resolve().parent.parent;cache=root/'app/stats/page_cache.py';db=root/'app/database.py';data=root/'app/stats/page_data.py'
controls=[
 ('warm-copies',cache,'return copy.deepcopy(self.entries[key][0])','return self.entries[key][0]','test_prepares_all_presets_accounts_and_mutable_results_are_private'),
 ('external-generation',cache,'if version != self._version or calendar != self._calendar:', 'if calendar != self._calendar:','test_external_commit_refreshes_other_periods_and_preserves_query_contracts'),
 ('account-generation',cache,"changed = {uid for uid, generation in self._accounts.items() if accounts.get(uid) != generation}", 'changed = set()','test_account_recreation_with_identical_fields_forgets_only_old_interests'),
 ('calendar-notification',cache,'if current != self._calendar:\n            self.notify()', 'if False:\n            self.notify()','test_idle_calendar_change_rebuilds_presets_and_customs_without_a_page_request'),
 ('background-ownership',cache,'if self._preparing and self._connection is not None:', 'if self._connection is not None:','test_readers_preempt_only_background_and_completed_queries_survive'),
 ('retention-generation',cache,'if (after != self._version or self._identity != await asyncio.to_thread(self._file_identity)', 'if (False or self._identity != await asyncio.to_thread(self._file_identity)','test_commit_during_interrupted_snapshot_discards_completed_queries'),
 ('exception-cleanup',cache,'                await self._drop_connection()\n                raise\n            finally:', '                pass\n                raise\n            finally:','test_cancelled_setup_and_failed_validation_drop_connection_and_recover'),
 ('restart-interests',cache,'        self.periods.clear()\n        self._calendar = None','        # omitted interest reset\n        self._calendar = None','test_restart_forgets_custom_periods_and_rebuilds_restored_history'),
 ('memory-budget',cache,'if amount <= self.MAX_BYTES:', 'if True:','test_budget_overflow_returns_correct_data_and_invalid_input_retains_nothing'),
 ('write-notification',db,'    statistics_cache.notify()','    pass # omitted stats notification','test_application_commits_notify_only_after_writer_lock_released'),
 ('stream-priority',cache,'        cache.reader_enter()\n        try:', '        # omitted priority\n        try:','test_stream_lifetime_and_failure_release_reader_priority'),
 ('path-switch',cache,"if self._path is not None and self._path != identity[0]:", 'if False:','test_path_switch_after_reader_failure_forgets_old_custom_interests'),
 ('readonly-snapshot',cache,"await self._connection.execute('PRAGMA query_only=ON')", "await self._connection.execute('PRAGMA query_only=OFF')",'test_generation_backfill_is_idempotent_and_private_reader_cannot_write'),
 ('timing-warm-reuse',cache,'if key in self.entries:', 'if False:','StatsPhases.test_stats_boundaries_default_off_and_cache_identity'),
]
results=[]
artifacts=Path(tempfile.mkdtemp(prefix='stats-cache-controls-'))
for name,path,before,after,method in controls:
 original=path.read_text();assert original.count(before)==1,(name,original.count(before))
 try:
  # Timestamp/size bytecode validation can confuse rapid, equal-size mutations.
  for compiled in (path.parent/'__pycache__').glob(path.stem+'.*.pyc'):compiled.unlink()
  path.write_text(original.replace(before,after))
  target=('tests.test_stats_timing.'+method if method.startswith('StatsPhases') else 'tests.test_statistics_cache.StatisticsCacheTests.'+method)
  r=subprocess.run([sys.executable,'-m','scripts.test_backend',target],cwd=root,env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1','APP_DATA_DIR':os.environ.get('APP_DATA_DIR',str(artifacts/'data'))},capture_output=True,text=True,timeout=25)
  log=artifacts/(name+'.log');log.write_text(r.stdout+r.stderr)
  assert r.returncode!=0,('Defect escaped test',name)
  assert 'AssertionError' in r.stderr or 'AssertionError' in r.stdout,('Unexpected setup/error instead of assertion',name,(r.stdout+r.stderr)[-1000:])
  print('REJECTED',name,flush=True);results.append({'guard':name,'test':target,'rejected':True})
 finally:
  path.write_text(original)
  for compiled in (path.parent/'__pycache__').glob(path.stem+'.*.pyc'):compiled.unlink()
(artifacts/'results.json').write_text(json.dumps(results,indent=2))
