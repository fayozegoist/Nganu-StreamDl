package main

// Pool proxy untuk resolve metadata (UC / Videy / Subtitle / gid).
// Sumber: env PROXIES + /proxy (proxies.json). Maks 10, dipilih acak.
// Download & upload TIDAK lewat proxy.

import (
	"encoding/json"
	"fmt"
	"log"
	"math/rand/v2"
	"net/http"
	"net/url"
	"os"
	"slices"
	"strconv"
	"strings"
	"sync"
	"time"
)

const (
	proxyFile  = "proxies.json"
	maxProxies = 10
)

var (
	proxyMu   sync.Mutex
	proxies   []string // URL http://user:pass@ip:port
	proxyPool = map[string]*http.Client{}
)

// normalizeProxy: terima "ip:port:user:pass" atau URL http.
func normalizeProxy(p string) (string, error) {
	p = strings.TrimSpace(p)
	if p == "" {
		return "", fmt.Errorf("kosong")
	}
	if !strings.HasPrefix(p, "http://") && !strings.HasPrefix(p, "https://") {
		parts := strings.Split(p, ":")
		if len(parts) == 4 {
			p = fmt.Sprintf("http://%s:%s@%s:%s", parts[2], parts[3], parts[0], parts[1])
		} else {
			p = "http://" + p
		}
	}
	u, err := url.Parse(p)
	if err != nil || u.Host == "" || !strings.Contains(u.Host, ":") {
		return "", fmt.Errorf("format proxy tidak valid")
	}
	return p, nil
}

// maskProxy: sembunyikan kredensial untuk tampilan chat/log.
func maskProxy(p string) string {
	if u, err := url.Parse(p); err == nil && u.Host != "" {
		return u.Host
	}
	return p
}

func loadProxies() {
	var out []string
	seen := map[string]bool{}
	add := func(raw string) {
		for _, part := range strings.Split(raw, ",") {
			part = strings.TrimSpace(part)
			if part == "" || seen[part] {
				continue
			}
			if px, err := normalizeProxy(part); err == nil {
				seen[part] = true
				seen[px] = true
				out = append(out, px)
			} else {
				log.Printf("proxy diabaikan (%s): %v", part, err)
			}
		}
	}
	// proxies.json (dikelola /proxy) otoritatif bila ada — env hanya bootstrap.
	if b, err := os.ReadFile(proxyFile); err == nil {
		var saved []string
		if json.Unmarshal(b, &saved) != nil {
			log.Printf("proxies.json rusak — pakai env")
			add(os.Getenv("PROXIES"))
			add(os.Getenv("GID_PROXIES"))
		} else {
			for _, p := range saved {
				add(p)
			}
		}
	} else {
		add(os.Getenv("PROXIES"))
		add(os.Getenv("GID_PROXIES")) // lama
	}
	if len(out) > maxProxies {
		out = out[:maxProxies]
	}
	proxyMu.Lock()
	proxies = out
	proxyPool = map[string]*http.Client{}
	proxyMu.Unlock()
	log.Printf("proxy pool: %d entri", len(out))
}

func saveProxies() error {
	proxyMu.Lock()
	b, _ := json.Marshal(proxies)
	proxyMu.Unlock()
	tmp := proxyFile + ".tmp"
	if err := os.WriteFile(tmp, b, 0o600); err != nil {
		return err
	}
	return os.Rename(tmp, proxyFile)
}

func addProxy(raw string) error {
	px, err := normalizeProxy(raw)
	if err != nil {
		return err
	}
	proxyMu.Lock()
	defer proxyMu.Unlock()
	if slices.Contains(proxies, px) {
		return fmt.Errorf("proxy sudah ada")
	}
	if len(proxies) >= maxProxies {
		return fmt.Errorf("maksimal %d proxy", maxProxies)
	}
	proxies = append(proxies, px)
	return nil
}

func delProxy(n int) (string, error) {
	proxyMu.Lock()
	defer proxyMu.Unlock()
	if n < 1 || n > len(proxies) {
		return "", fmt.Errorf("nomor %d tidak ada", n)
	}
	removed := proxies[n-1]
	proxies = slices.Delete(proxies, n-1, n)
	return removed, nil
}

func clearProxies() int {
	proxyMu.Lock()
	defer proxyMu.Unlock()
	n := len(proxies)
	proxies = nil
	return n
}

func proxySnapshot() []string {
	proxyMu.Lock()
	defer proxyMu.Unlock()
	return slices.Clone(proxies)
}

func pickProxy() *url.URL {
	list := proxySnapshot()
	if len(list) == 0 {
		return nil
	}
	u, err := url.Parse(list[rand.IntN(len(list))])
	if err != nil {
		return nil
	}
	return u
}

func clientFor(u *url.URL) *http.Client {
	key := u.String()
	proxyMu.Lock()
	defer proxyMu.Unlock()
	if c, ok := proxyPool[key]; ok {
		return c
	}
	c := &http.Client{
		Transport: &http.Transport{Proxy: http.ProxyURL(u)},
		Timeout:   25 * time.Second,
	}
	proxyPool[key] = c
	return c
}

// doScrape: request lewat proxy acak; gagal → langsung (fitur lama tak boleh mati).
func doScrape(req *http.Request) (*http.Response, error) {
	if px := pickProxy(); px != nil {
		r := req.Clone(req.Context())
		if req.GetBody != nil {
			if b, err := req.GetBody(); err == nil {
				r.Body = b
			}
		}
		resp, err := clientFor(px).Do(r)
		if err == nil {
			return resp, nil
		}
		log.Printf("proxy %s gagal (%v) — direct", maskProxy(px.String()), err)
	}
	return scrapeHTTP.Do(req)
}

// handleProxyCmd: /proxy [list|add <fmt>|del <n>|clear] — admin saja.
func handleProxyCmd(chat int64, arg string) {
	fields := strings.Fields(arg)
	sub := "list"
	if len(fields) > 0 {
		sub = strings.ToLower(fields[0])
	}
	switch sub {
	case "add":
		rest := strings.TrimSpace(strings.TrimPrefix(strings.TrimSpace(arg), fields[0]))
		if rest == "" {
			sendHTML(chat, "Format: <code>/proxy add ip:port:user:pass</code>")
			return
		}
		if err := addProxy(rest); err != nil {
			sendHTML(chat, "❌ "+esc(err.Error()))
			return
		}
		if err := saveProxies(); err != nil {
			log.Printf("simpan proxy gagal: %v", err)
		}
		list := proxySnapshot()
		log.Printf("proxy ditambah: %s (total %d)", maskProxy(list[len(list)-1]), len(list))
		sendHTML(chat, fmt.Sprintf("✅ Proxy ditambah: <code>%s</code>\nTotal %d/%d",
			esc(maskProxy(list[len(list)-1])), len(list), maxProxies))
	case "del", "delete", "rm":
		n, _ := strconv.Atoi(strings.TrimSpace(strings.TrimPrefix(strings.TrimSpace(arg), fields[0])))
		removed, err := delProxy(n)
		if err != nil {
			sendHTML(chat, "❌ "+esc(err.Error()))
			return
		}
		if err := saveProxies(); err != nil {
			log.Printf("simpan proxy gagal: %v", err)
		}
		sendHTML(chat, fmt.Sprintf("🗑 Proxy <code>%s</code> dihapus. Sisa %d.",
			esc(maskProxy(removed)), len(proxySnapshot())))
	case "clear":
		n := clearProxies()
		if err := saveProxies(); err != nil {
			log.Printf("simpan proxy gagal: %v", err)
		}
		sendHTML(chat, fmt.Sprintf("🧹 %d proxy dihapus — semua resolve lewat direct.", n))
	default:
		list := proxySnapshot()
		if len(list) == 0 {
			sendHTML(chat, "Belum ada proxy — semua resolve lewat direct.\nTambah: <code>/proxy add ip:port:user:pass</code>")
			return
		}
		var b strings.Builder
		fmt.Fprintf(&b, "<b>Proxy pool</b> (%d/%d, acak per request)\n", len(list), maxProxies)
		for i, p := range list {
			fmt.Fprintf(&b, "%d. <code>%s</code>\n", i+1, esc(maskProxy(p)))
		}
		b.WriteString("\n<code>/proxy add ip:port:user:pass</code>\n<code>/proxy del n</code> · <code>/proxy clear</code>")
		sendHTML(chat, b.String())
	}
}
