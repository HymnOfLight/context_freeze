"""Ubuntu 22.04 (cgroup v2 + X11) back-end: *throttle* background apps instead of freezing them.

An app whose window loses focus keeps running - its processes, windows, sockets and in-memory
state are untouched - but it is moved to a resource tier:

    fg       the focused app: no limits
    bg       CPU-limited (default one logical core worth of cpu.max quota, low cpu.weight)
    shrunk   CPU-limited + memory.high = mem_frac * m_fg (pages are reclaimed to swap / zram) +
             optional memory.swap.max

Nothing is ever frozen or killed: memory.max stays unlimited unless `mem_hard` is set, so the
kernel throttles allocations instead of OOM-killing, and the application's context survives.

Modules
    cgroup       cgroup v2 tree under /sys/fs/cgroup/cf (delegated to the desktop user)
    procs        /proc sampling (smaps_rollup, stat, io) and system counters
    x11          window discovery / focus switching / _NET_WM_PING responsiveness
    notify       counts desktop notifications per app (org.freedesktop.Notifications)
    apps         catalogue of the mainland-China Linux desktop apps + usage scenarios
    device       LinuxDesktop: launch / attach, tier changes, sampling, state checks
    runner       the experiment loop (checkpoint / resume, JSONL)
    analyze      summary of the Linux-specific metrics
    focusd       daemon that applies the tiers on a live desktop whenever focus changes
    placeholder_app  Tk stand-in with a verifiable progress counter (for apps without a Linux build)
"""
