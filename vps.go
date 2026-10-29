package main

// /infovps — panel admin: statistik pemakaian VPS (Linux /proc + statfs).

import (
	"fmt"
	"os"
	"runtime"
	"strconv"
	"strings"
	"syscall"
)

func readProc(p string) string {
	b, err := os.ReadFile(p)
	if err != nil {
		return ""
	}
	return string(b)
}

func cpuModel() string {
	for _, l := range strings.Split(readProc("/proc/cpuinfo"), "\n") {
		if strings.HasPrefix(l, "model name") {
			if i := strings.Index(l, ":"); i > 0 {
				return strings.TrimSpace(l[i+1:])
			}
		}
	}
	return "-"
}

func memStats() (total, avail, swapTotal, swapFree int64) {
	for _, l := range strings.Split(readProc("/proc/meminfo"), "\n") {
		f := strings.Fields(l)
		if len(f) < 2 {
			continue
		}
		v, _ := strconv.ParseInt(f[1], 10, 64)
		v *= 1024 // kB → B
		switch f[0] {
		case "MemTotal:":
			total = v
		case "MemAvailable:":
			avail = v
		case "SwapTotal:":
			swapTotal = v
		case "SwapFree:":
			swapFree = v
		}
	}
	return
}

func netTotals() (rx, tx int64) {
	for _, l := range strings.Split(readProc("/proc/net/dev"), "\n") {
		i := strings.Index(l, ":")
		if i < 0 {
			continue
		}
		if strings.TrimSpace(l[:i]) == "lo" {
			continue
		}
		f := strings.Fields(l[i+1:])
		if len(f) < 9 {
			continue
		}
		r, _ := strconv.ParseInt(f[0], 10, 64)
		t, _ := strconv.ParseInt(f[8], 10, 64)
		rx += r
		tx += t
	}
	return
}

func fmtDurSec(sec float64) string {
	s := int64(sec)
	d, h, m := s/86400, (s%86400)/3600, (s%3600)/60
	if d > 0 {
		return fmt.Sprintf("%dd %dj %dm", d, h, m)
	}
	return fmt.Sprintf("%dj %dm", h, m)
}

func selfRSS() int64 {
	for _, l := range strings.Split(readProc("/proc/self/status"), "\n") {
		if strings.HasPrefix(l, "VmRSS:") {
			f := strings.Fields(l)
			v, _ := strconv.ParseInt(f[1], 10, 64)
			return v * 1024
		}
	}
	return 0
}

func vpsInfo() string {
	host, _ := os.Hostname()
	osName := "-"
	for _, l := range strings.Split(readProc("/etc/os-release"), "\n") {
		if strings.HasPrefix(l, "PRETTY_NAME=") {
			osName = strings.Trim(strings.TrimPrefix(l, "PRETTY_NAME="), `"`)
		}
	}
	kernel := strings.TrimSpace(readProc("/proc/sys/kernel/osrelease"))

	var upSec float64
	fmt.Sscanf(readProc("/proc/uptime"), "%f", &upSec)
	loads := strings.Fields(readProc("/proc/loadavg"))
	load := "-"
	if len(loads) >= 3 {
		load = strings.Join(loads[:3], " ")
	}

	mt, ma, st_, sf := memStats()
	ramUsed := mt - ma
	ramPct := int64(0)
	if mt > 0 {
		ramPct = ramUsed * 100 / mt
	}

	var fs syscall.Statfs_t
	var dTotal, dFree int64
	if syscall.Statfs("/", &fs) == nil {
		dTotal = int64(fs.Blocks) * fs.Bsize
		dFree = int64(fs.Bavail) * fs.Bsize
	}
	dUsed := dTotal - dFree
	dPct := int64(0)
	if dTotal > 0 {
		dPct = dUsed * 100 / dTotal
	}

	rx, tx := netTotals()

	return fmt.Sprintf(
		"📊 <b>Info VPS</b>\n\n"+
			"🖥 <b>%s</b>\n　　%s • %s • %s\n"+
			"⏱ Uptime: %s\n"+
			"🧠 CPU: %s (%d core)\n　　Load: %s\n"+
			"💾 RAM: %s / %s (%d%%)\n　　Swap: %s / %s\n"+
			"🗄 Disk /: %s / %s (%d%%)\n　　Sisa: %s\n"+
			"🌐 Net total: ⬇️ %s • ⬆️ %s\n"+
			"🤖 Bot RSS: %s",
		esc(host), esc(osName), esc(kernel), runtime.GOARCH,
		fmtDurSec(upSec),
		esc(cpuModel()), runtime.NumCPU(), load,
		fmtSize(ramUsed), fmtSize(mt), ramPct,
		fmtSize(st_-sf), fmtSize(st_),
		fmtSize(dUsed), fmtSize(dTotal), dPct, fmtSize(dFree),
		fmtSize(rx), fmtSize(tx),
		fmtSize(selfRSS()))
}
