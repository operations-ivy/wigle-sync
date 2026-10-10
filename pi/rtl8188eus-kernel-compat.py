#!/usr/bin/env python3
"""Patch aircrack-ng/rtl8188eus (af3bf00) to build on kernels up to 6.18.

Run by setup-rtl8188eus.sh against the DKMS source tree. Idempotent.
"""
import os
import sys

root = sys.argv[1]


def patch(rel, edits):
    global s
    p = os.path.join(root, rel)
    s = open(p, newline="").read()
    edits()
    open(p, "w", newline="").write(s)


def sub(old, new):
    global s
    if new in s:
        return
    assert s.count(old) == 1, old
    s = s.replace(old, new)


def makefile():
    # 6.15 dropped EXTRA_CFLAGS, so the driver's include paths went missing.
    global s
    s = s.replace("EXTRA_CFLAGS", "ccflags-y")


def timers():
    # 6.15 renamed del_timer*; 6.16 renamed from_timer.
    sub("#include <linux/version.h>\n", """#include <linux/version.h>
#if LINUX_VERSION_CODE >= KERNEL_VERSION(6, 15, 0)
#ifndef del_timer_sync
#define del_timer_sync(t) timer_delete_sync(t)
#endif
#ifndef del_timer
#define del_timer(t) timer_delete(t)
#endif
#endif
#if LINUX_VERSION_CODE >= KERNEL_VERSION(6, 16, 0) && !defined(from_timer)
#define from_timer(var, callback_timer, timer_fieldname) timer_container_of(var, callback_timer, timer_fieldname)
#endif
""")


def cfg80211():
    # 6.17: radio_idx added to set_wiphy_params
    sub("static int cfg80211_rtw_set_wiphy_params(struct wiphy *wiphy, u32 changed)\n",
        "static int cfg80211_rtw_set_wiphy_params(struct wiphy *wiphy,\n"
        "#if (LINUX_VERSION_CODE >= KERNEL_VERSION(6, 17, 0))\n\tint radio_idx,\n#endif\n\tu32 changed)\n")

    # 6.17: radio_idx added to set_tx_power
    sub("""static int cfg80211_rtw_set_txpower(struct wiphy *wiphy,
#if (LINUX_VERSION_CODE >= KERNEL_VERSION(3, 8, 0))
	struct wireless_dev *wdev,
#endif
""", """static int cfg80211_rtw_set_txpower(struct wiphy *wiphy,
#if (LINUX_VERSION_CODE >= KERNEL_VERSION(3, 8, 0))
	struct wireless_dev *wdev,
#endif
#if (LINUX_VERSION_CODE >= KERNEL_VERSION(6, 17, 0))
	int radio_idx,
#endif
""")

# 6.14: link_id, 6.17: radio_idx added to get_tx_power
    sub("""static int cfg80211_rtw_get_txpower(struct wiphy *wiphy,
#if (LINUX_VERSION_CODE >= KERNEL_VERSION(3, 8, 0))
	struct wireless_dev *wdev,
#endif
	int *dbm)""", """static int cfg80211_rtw_get_txpower(struct wiphy *wiphy,
#if (LINUX_VERSION_CODE >= KERNEL_VERSION(3, 8, 0))
	struct wireless_dev *wdev,
#endif
#if (LINUX_VERSION_CODE >= KERNEL_VERSION(6, 17, 0))
	int radio_idx,
#endif
#if (LINUX_VERSION_CODE >= KERNEL_VERSION(6, 14, 0))
	unsigned int link_id,
#endif
	int *dbm)""")

# 6.13: net_device added to set_monitor_channel
    sub("""static int cfg80211_rtw_set_monitor_channel(struct wiphy *wiphy
#if (LINUX_VERSION_CODE >= KERNEL_VERSION(3, 8, 0))
	, struct cfg80211_chan_def *chandef""", """static int cfg80211_rtw_set_monitor_channel(struct wiphy *wiphy
#if (LINUX_VERSION_CODE >= KERNEL_VERSION(6, 13, 0))
	, struct net_device *dev
#endif
#if (LINUX_VERSION_CODE >= KERNEL_VERSION(3, 8, 0))
	, struct cfg80211_chan_def *chandef""")


patch("Makefile", makefile)
patch("include/osdep_service_linux.h", timers)
patch("os_dep/linux/ioctl_cfg80211.c", cfg80211)
print("patched", root)
