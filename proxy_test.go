package main

import "testing"

func TestProxyPool(t *testing.T) {
	clearProxies()
	if p := pickProxy(); p != nil {
		t.Fatal("pickProxy harus nil saat pool kosong")
	}

	if err := addProxy("1.2.3.4:8080:user:pass"); err != nil {
		t.Fatal(err)
	}
	got := proxySnapshot()[0]
	if got != "http://user:pass@1.2.3.4:8080" {
		t.Fatalf("normalisasi salah: %q", got)
	}
	if m := maskProxy(got); m != "1.2.3.4:8080" {
		t.Fatalf("mask salah: %q", m)
	}
	if err := addProxy("1.2.3.4:8080:user:pass"); err == nil {
		t.Fatal("duplikat harus ditolak")
	}

	for i := 0; i < 20; i++ {
		_ = addProxy("10.0.0." + string(rune('a'+i)) + ":80:u:p")
	}
	if n := len(proxySnapshot()); n != maxProxies {
		t.Fatalf("batas pool salah: %d != %d", n, maxProxies)
	}

	if _, err := delProxy(1); err != nil {
		t.Fatal(err)
	}
	if n := len(proxySnapshot()); n != maxProxies-1 {
		t.Fatalf("del gagal: %d", n)
	}
	if _, err := delProxy(999); err == nil {
		t.Fatal("nomor invalid harus error")
	}

	if n := clearProxies(); n != maxProxies-1 {
		t.Fatalf("clear salah: %d", n)
	}
	if p := pickProxy(); p != nil {
		t.Fatal("pool harus kosong setelah clear")
	}
}
