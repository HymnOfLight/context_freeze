from cf import parsers as P

SMAPS = """00400000-7fffffffffff ---p 00000000 00:00 0                              [rollup]
Rss:              123456 kB
Pss:               98765 kB
Pss_Dirty:         50000 kB
Pss_Anon:          60000 kB
Pss_File:          38000 kB
Pss_Shmem:           765 kB
Shared_Clean:      20000 kB
Private_Dirty:     50000 kB
Anonymous:         61000 kB
Swap:              20480 kB
SwapPss:           20000 kB
Locked:                0 kB
"""

AM_START = """Starting: Intent { act=android.intent.action.MAIN cat=[android.intent.category.LAUNCHER] cmp=com.android.settings/.Settings }
Status: ok
LaunchState: WARM
Activity: com.android.settings/.Settings
TotalTime: 412
WaitTime: 430
Complete
"""

AM_HOT = """Starting: Intent { cmp=com.google.android.dialer/.extensions.GoogleDialtactsActivity }
Warning: Activity not started, its current task has been brought to the front
Status: ok
LaunchState: HOT
Activity: com.google.android.dialer/com.android.dialer.main.impl.MainActivity
TotalTime: 7391
WaitTime: 7778
Complete
"""

AM_FRONT = """Starting: Intent { cmp=com.google.android.apps.messaging/.ui.ConversationListActivity }
Warning: Activity not started, intent has been delivered to currently running top-most instance.
Status: ok
LaunchState: UNKNOWN (0)
Activity: com.google.android.apps.messaging/.gaia.expresssignin.BugleExpressSignInActivity
TotalTime: 0
WaitTime: 5
Complete
"""

AM_TIMEOUT = """Starting: Intent { cmp=com.google.android.deskclock/com.android.deskclock.DeskClock }
Status: timeout
LaunchState: UNKNOWN (-1)
Activity: com.google.android.deskclock/com.android.deskclock.DeskClock
WaitTime: 27792
Complete
"""


def test_smaps_rollup():
    kv = P.parse_kv_kb(SMAPS)
    assert kv["Pss"] == 98765 and kv["Pss_Anon"] == 60000 and kv["SwapPss"] == 20000
    assert kv["Pss_File"] == 38000


def test_am_start():
    r = P.parse_am_start(AM_START)
    assert r["status"] == "ok" and r["launch_state"] == "WARM"
    assert r["total_time_ms"] == 412 and r["wait_time_ms"] == 430
    hot = P.parse_am_start(AM_HOT)
    assert hot["launch_state"] == "HOT" and hot["total_time_ms"] == 7391
    front = P.parse_am_start(AM_FRONT)
    assert front["launch_state"] == "FRONT" and front["total_time_ms"] is None
    to = P.parse_am_start(AM_TIMEOUT)
    assert to["launch_state"] == "TIMEOUT" and to["total_time_ms"] == 27792


def test_mm_stat_psi_swaps_vmstat():
    z = P.parse_zram_mm_stat("104857600 31457280 33554432 0 33554432 100 0 5 5\n")
    assert z["orig_data_size"] == 104857600 and z["mem_used_total"] == 33554432
    psi = P.parse_psi("some avg10=1.50 avg60=0.80 avg300=0.10 total=123456\nfull avg10=0.00 avg60=0.00 avg300=0.00 total=789\n")
    assert psi["some_avg10"] == 1.5 and psi["full_total"] == 789
    sw = P.parse_swaps("Filename\t\t\t\tType\t\tSize\t\tUsed\t\tPriority\n/dev/block/zram0                        partition\t1048572\t\t2048\t\t32767\n")
    assert sw[0]["filename"] == "/dev/block/zram0" and sw[0]["priority"] == 32767
    vm = P.parse_vmstat("nr_free_pages 1000\npswpin 12\npswpout 34\nworkingset_refault_anon 5\n")
    assert vm["pswpout"] == 34 and vm["workingset_refault_anon"] == 5


def test_proc_stat_majflt():
    stat = "1234 (com.foo bar:svc) S 1 1 0 0 -1 4194560 5000 0 42 0 10 5 0 0 20 0 30 0 100 1000000 500 18446744073709551615"
    assert P.parse_proc_stat_majflt(stat) == 42


def test_resolve_activity():
    out = "priority=0 preferredOrder=0 match=0x108000 specificIndex=-1 isDefault=true\ncom.android.settings/.Settings\n"
    assert P.parse_resolve_activity(out) == "com.android.settings/.Settings"
    assert P.parse_resolve_activity("No activity found\n") is None
