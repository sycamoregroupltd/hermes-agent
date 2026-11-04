"""Same-ID lost updates: only temporary stores, no agents or delivery."""
import multiprocessing

import pytest

from cron import jobs


def _seed(home):
    with jobs.use_cron_store(home):
        jobs.save_jobs([{
            'id': 'r15-test', 'name': 'isolated test', 'enabled': False,
            'state': 'paused', 'prompt': 'test',
            'schedule': {'kind': 'interval', 'minutes': 60, 'display': 'every 1h'},
            'failure_deliver': 'local',
        }])


def _stale_writer(home, pipe):
    with jobs.use_cron_store(home):
        snapshot = jobs.load_jobs()
        pipe.send('loaded')
        assert pipe.recv() == 'save'
        snapshot[0]['last_run_at'] = '2026-11-04T10:00:00+00:00'
        snapshot[0]['last_status'] = 'ok'
        jobs.save_jobs(snapshot)
        # A second run-state save from the same long-held read must also keep
        # the authored route; persisting once must not certify stale fields.
        snapshot[0]['last_status'] = 'error'
        jobs.save_jobs(snapshot)
        pipe.send('saved')
    pipe.close()


@pytest.mark.skipif('fork' not in multiprocessing.get_all_start_methods(),
                    reason='This interleaving test requires POSIX fork')
def test_cross_process_stale_run_state_save_preserves_native_route_edit(tmp_path):
    """A write lock alone cannot make an earlier read fresh."""
    _seed(tmp_path)
    ctx = multiprocessing.get_context('fork')
    parent, child = ctx.Pipe()
    worker = ctx.Process(target=_stale_writer, args=(tmp_path, child))
    worker.start()
    child.close()
    try:
        assert parent.poll(15) and parent.recv() == 'loaded'
        with jobs.use_cron_store(tmp_path):
            jobs.update_job('r15-test', {'failure_deliver': 'local,telegram:test-destination'})
        parent.send('save')
        assert parent.poll(15) and parent.recv() == 'saved'
        worker.join(15)
        assert worker.exitcode == 0
        with jobs.use_cron_store(tmp_path):
            result = jobs.load_jobs()[0]
        assert result['failure_deliver'] == 'local,telegram:test-destination'
        assert result['last_status'] == 'error'
        assert result['last_run_at'] == '2026-11-04T10:00:00+00:00'
    finally:
        if worker.is_alive():
            worker.terminate()
            worker.join(15)
        parent.close()


def test_disjoint_authored_changes_merge_and_explicit_revert_works(tmp_path):
    _seed(tmp_path)
    with jobs.use_cron_store(tmp_path):
        stale = jobs.load_jobs()
        jobs.update_job('r15-test', {'failure_deliver': 'telegram:test-destination'})
        stale[0]['name'] = 'intentional rename'
        jobs.save_jobs(stale)
        result = jobs.load_jobs()[0]
        assert result['name'] == 'intentional rename'
        assert result['failure_deliver'] == 'telegram:test-destination'
        jobs.update_job('r15-test', {'failure_deliver': 'local'})
        assert jobs.load_jobs()[0]['failure_deliver'] == 'local'


def test_deep_baseline_preserves_concurrent_nested_edit(tmp_path):
    _seed(tmp_path)
    with jobs.use_cron_store(tmp_path):
        stale = jobs.load_jobs()
        fresh = jobs.load_jobs()
        fresh[0]['schedule']['minutes'] = 120
        jobs.save_jobs(fresh)
        stale[0]['last_status'] = 'ok'
        jobs.save_jobs(stale)
        assert jobs.load_jobs()[0]['schedule']['minutes'] == 120


def test_optional_field_removal_does_not_get_restored_by_stale_save(tmp_path):
    _seed(tmp_path)
    with jobs.use_cron_store(tmp_path):
        stale = jobs.load_jobs()
        fresh = jobs.load_jobs()
        del fresh[0]['failure_deliver']
        jobs.save_jobs(fresh)
        stale[0]['last_status'] = 'ok'
        jobs.save_jobs(stale)
        assert 'failure_deliver' not in jobs.load_jobs()[0]


def test_snapshot_cannot_write_a_different_profile(tmp_path):
    _seed(tmp_path / 'a')
    _seed(tmp_path / 'b')
    with jobs.use_cron_store(tmp_path / 'a'):
        snapshot = jobs.load_jobs()
    with jobs.use_cron_store(tmp_path / 'b'):
        with pytest.raises(ValueError, match='different cron store'):
            jobs.save_jobs(snapshot)


def test_plain_list_keeps_explicit_replacement_semantics(tmp_path):
    _seed(tmp_path)
    with jobs.use_cron_store(tmp_path):
        plain = list(jobs.load_jobs())
        plain[0]['failure_deliver'] = 'telegram:test-destination'
        jobs.save_jobs(plain)
        assert jobs.load_jobs()[0]['failure_deliver'] == 'telegram:test-destination'
