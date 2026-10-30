package main

// Cek resolver nyata (butuh internet): go test -run TestLive -v
// Network-flaky OK — ini smoke test manual, bukan CI gate.

import (
	"os"
	"strings"
	"testing"
)

func TestLiveTeraboxPrimary(t *testing.T) {
	items, err := fetchTbox("https://1024terabox.com/s/1MX1G_KF-LlE0iH0rQRKe1Q")
	if err != nil {
		t.Skipf("tbox erreur: %v", err)
	}
	if len(items) == 0 {
		t.Fatal("items kosong")
	}
	t.Logf("%d item: %s (%s)", len(items), items[0].Title, items[0].SizeLabel)
	if items[0].URL == "" {
		t.Fatal("URL kosong")
	}
}

// Chain baru: getindevice dulu — cetak satu URL download buat uji curl manual.
func TestLiveGidChain(t *testing.T) {
	items, err := resolveTerabox("https://1024terabox.com/s/1fshWBnhymSAqcBatuI9PIA")
	if err != nil {
		t.Skipf("resolve gagal: %v", err)
	}
	if len(items) == 0 {
		t.Fatal("items kosong")
	}
	t.Logf("total %d item — pertama: %s", len(items), items[0].Title)
	for _, it := range items {
		if it.URL != "" && strings.HasSuffix(strings.ToLower(it.Title), ".mp4") {
			t.Logf("probe-url: %s", it.URL)
			return
		}
	}
	for _, it := range items {
		if it.HLS != "" {
			t.Logf("probe-hls: %s", it.HLS)
			return
		}
	}
	t.Log("semua item HLS (tanpa URL direct) — cek hls:")
	t.Logf("hls: %s", items[0].HLS)
}

// Chain folder-share (butuh /gid uploader + proxy): 3 subfolder → 80+ item.
func TestLiveGidFolder(t *testing.T) {
	items, err := resolveTerabox("https://1024terabox.com/s/1HxF4kd3MRc8bcikLF2P_6A")
	if err != nil {
		t.Skipf("resolve gagal: %v", err)
	}
	if len(items) < 20 {
		t.Fatalf("folder share harus >20 item, dapat %d", len(items))
	}
	t.Logf("total %d item — pertama: %s (%s)", len(items), items[0].Title, items[0].SizeLabel)
}

// Uji resolver via proxy acak (butuh PROXIES di env): go test -run TestLiveProxy -v
func TestLiveProxyResolvers(t *testing.T) {
	if os.Getenv("PROXIES") == "" && os.Getenv("GID_PROXIES") == "" {
		t.Skip("PROXIES kosong")
	}
	loadProxies()
	if len(proxySnapshot()) == 0 {
		t.Skip("pool kosong")
	}
	items, err := resolveUC("https://uc-share.com/s/fb5db48e48904?la=id")
	if err != nil {
		t.Fatalf("UC via proxy gagal: %v", err)
	}
	t.Logf("UC via proxy: %d item", len(items))
	subs, err := searchSubtitle("naruto")
	if err != nil {
		t.Fatalf("subtitle via proxy gagal: %v", err)
	}
	t.Logf("subtitle via proxy: %d hasil", len(subs))
}

func TestLiveSubtitle(t *testing.T) {
	items, err := searchSubtitle("naruto")
	if err != nil {
		t.Skipf("subtitle error: %v", err)
	}
	if len(items) == 0 {
		t.Fatal("hasil kosong")
	}
	t.Logf("%d hasil: %s", len(items), items[0].Title)
	if items[0].Detail == "" {
		t.Fatal("detail kosong")
	}
}

func TestLiveUC(t *testing.T) {
	// Test hanya regex (tanpa network) — share publik tidak stabil.
	if m := ucPwdRe1.FindStringSubmatch("https://uc-share.com/s/abc123?pwd=x"); m == nil || m[1] != "abc123" {
		t.Fatal("regex pwd gagal")
	}
}

func TestBuildFullCDN(t *testing.T) {
	seg := "https://dl-worker.teraboxdl.site/api/media-download?url=" +
		"https%3A%2F%2Fv4.freeterabox.com%2Fvideo%2Fx%3Fts_size%3D1000%26len%3D500%26fn%3Dtest.mp4"
	full := buildFullCDN(seg)
	if full == "" {
		t.Fatal("buildFullCDN gagal")
	}
	t.Log(full)
}
