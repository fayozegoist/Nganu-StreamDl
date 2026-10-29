package main

// Terabox resolver.
// Default: API getindevice.com native (token + /api/download/).
// Fallback: https://tbox-abf.pages.dev/?link=<url> (JSON: data.list[]).

import (
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"net/url"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"
)

var scrapeHTTP = &http.Client{Timeout: 30 * time.Second}

func fmtDur(sec int64) string {
	if sec <= 0 {
		return ""
	}
	h, rem := sec/3600, sec%3600
	m, s := rem/60, rem%60
	if h > 0 {
		return fmt.Sprintf("%d:%02d:%02d", h, m, s)
	}
	return fmt.Sprintf("%d:%02d", m, s)
}

var teraDomains = regexp.MustCompile(`(?i)(terabox\.com|1024terabox\.com|teraboxapp\.com|terasharelink\.com)`)

// ---------- API utama: tbox-abf.pages.dev ----------

type tboxResp struct {
	Status string `json:"status"`
	Data   struct {
		List []struct {
			Name         string `json:"name"`
			Size         int64  `json:"size"`
			SizeFmt      string `json:"size_formatted"`
			DownloadLink string `json:"download_link"`
			FastDownload string `json:"fast_download_link"`
			StreamURL    string `json:"stream_url"`
			DurationSec  int64  `json:"duration"`
			Thumbnail    string `json:"thumbnail"`
			Thumbs       struct {
				URL1 string `json:"url1"`
				URL3 string `json:"url3"`
			} `json:"thumbs"`
			IsDir int `json:"isdir"`
		} `json:"list"`
	} `json:"data"`
}

func fetchTbox(link string) ([]Item, error) {
	u := "https://tbox-abf.pages.dev/?link=" + url.QueryEscape(link)
	req, _ := http.NewRequest("GET", u, nil)
	req.Header.Set("User-Agent", "Mozilla/5.0")
	resp, err := scrapeHTTP.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	var r tboxResp
	if err := json.NewDecoder(io.LimitReader(resp.Body, 8<<20)).Decode(&r); err != nil {
		return nil, err
	}
	if r.Status != "success" {
		return nil, fmt.Errorf("tbox: status %s", r.Status)
	}
	var items []Item
	for _, f := range r.Data.List {
		if f.IsDir != 0 {
			continue
		}
		dl := f.DownloadLink
		if dl == "" {
			dl = f.FastDownload
		}
		stream := f.StreamURL
		if stream == "" {
			stream = dl
		}
		thumb := f.Thumbnail
		if thumb == "" {
			thumb = f.Thumbs.URL3
		}
		if thumb == "" {
			thumb = f.Thumbs.URL1
		}
		items = append(items, Item{
			Title:     f.Name,
			Size:      f.Size,
			SizeLabel: f.SizeFmt,
			Duration:  fmtDur(f.DurationSec),
			URL:       dl,
			Stream:    stream,
			Thumb:     thumb,
		})
	}
	return items, nil
}

// ---------- fallback: getindevice.com ----------

const gidBase = "https://getindevice.com"
const gidUA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"

var gidHeaders = map[string]string{
	"User-Agent": gidUA,
	"Referer":    gidBase + "/terabox-downloader/",
	"Origin":     gidBase,
}

func gidToken() (string, error) {
	u := fmt.Sprintf("%s/api/token/?_t=%d", gidBase, time.Now().UnixMilli())
	req, _ := http.NewRequest("GET", u, nil)
	for k, v := range gidHeaders {
		req.Header.Set(k, v)
	}
	resp, err := scrapeHTTP.Do(req)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()
	var r struct {
		Token string `json:"token"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&r); err != nil {
		return "", err
	}
	if r.Token == "" {
		return "", fmt.Errorf("token kosong")
	}
	return r.Token, nil
}

type gidItem struct {
	Order   int    `json:"order"`
	Quality string `json:"quality"`
	Format  string `json:"format"`
	IsHLS   bool   `json:"is_hls"`
	URL     string `json:"url"`
	Sig     string `json:"sig"`
	HLSURL  string `json:"hlsStreamUrl"`
}

func gidResolve(link, token string) (map[string]any, error) {
	payload := strings.NewReader(fmt.Sprintf(`{"url":%q}`, link))
	req, _ := http.NewRequest("POST", gidBase+"/api/download/", payload)
	for k, v := range gidHeaders {
		req.Header.Set(k, v)
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("x-request-token", token)
	resp, err := scrapeHTTP.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	var out map[string]any
	if err := json.NewDecoder(io.LimitReader(resp.Body, 16<<20)).Decode(&out); err != nil {
		return nil, err
	}
	return out, nil
}

func fixHLS(u string) string {
	if strings.Contains(u, "/api/hls-stream") && !strings.Contains(u, "/api/hls-stream/") {
		return strings.Replace(u, "/api/hls-stream?", "/api/hls-stream/?", 1)
	}
	return u
}

func gidFallback(link string) ([]Item, error) {
	var lastErr error
	for attempt := 0; attempt < 3; attempt++ {
		items, err := gidResolveOnce(link)
		if err == nil {
			return items, nil
		}
		lastErr = err
		time.Sleep(time.Duration(1+attempt*2) * time.Second)
	}
	return nil, fmt.Errorf("API getindevice gagal setelah 3x: %w", lastErr)
}

func gidResolveOnce(link string) ([]Item, error) {
	token, err := gidToken()
	if err != nil {
		return nil, err
	}
	data, err := gidResolve(link, token)
	if err != nil {
		return nil, err
	}
	var all []gidItem
	for _, kind := range []string{"videos", "photos", "audios"} {
		raw, _ := data[kind].([]any)
		for _, v := range raw {
			b, _ := json.Marshal(v)
			var it gidItem
			if json.Unmarshal(b, &it) == nil {
				all = append(all, it)
			}
		}
	}
	sort.Slice(all, func(i, j int) bool { return all[i].Order < all[j].Order })
	var items []Item
	for i, it := range all {
		label := it.Format
		if label == "" {
			label = "file"
		}
		entry := Item{
			Title: fmt.Sprintf("%s #%d (%s .%s)", label, i+1, it.Quality, it.Format),
		}
		if it.IsHLS && it.HLSURL != "" {
			entry.HLS = fixHLS(it.HLSURL)
			entry.Stream = entry.HLS
		} else if it.URL != "" {
			b64 := base64.URLEncoding.EncodeToString([]byte(it.URL))
			entry.URL = fmt.Sprintf("https://dl.getindevice.com/api/media-download?url=%s&ext=%s&platform=terabox&sig=%s",
				b64, it.Format, url.QueryEscape(it.Sig))
			entry.Direct = entry.URL
			if fn := basenameIfFile(it.URL); fn != "" {
				entry.Title = fn
			}
		} else {
			continue
		}
		items = append(items, entry)
	}
	// Judul asli + size + durasi dari playlist m3u8 — 1 fetch per item, concurrent.
	var wg sync.WaitGroup
	sem := make(chan struct{}, 4)
	for i := range items {
		if items[i].HLS == "" {
			if items[i].Direct != "" && items[i].Size == 0 {
				wg.Add(1)
				go func(i int) {
					defer wg.Done()
					sem <- struct{}{}
					defer func() { <-sem }()
					items[i].Size = headSizeRef(items[i].Direct, "")
				}(i)
			}
			continue
		}
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			sem <- struct{}{}
			defer func() { <-sem }()
			fn, size, durSec := hlsMeta(items[i].HLS)
			if fn != "" {
				items[i].Title = fn
			}
			if size > 0 {
				items[i].Size = size
			}
			if durSec > 0 {
				items[i].Duration = fmtDur(durSec)
			}
		}(i)
	}
	wg.Wait()
	return items, nil
}

var extFileRe = regexp.MustCompile(`(?i)\.[a-z0-9]{2,5}$`)

func basenameIfFile(rawURL string) string {
	p, err := url.Parse(rawURL)
	if err != nil {
		return ""
	}
	base := p.Path[strings.LastIndex(p.Path, "/")+1:]
	base, _ = url.PathUnescape(base)
	if base == "" || !extFileRe.MatchString(base) {
		return ""
	}
	return base
}

var extinfRe = regexp.MustCompile(`(?i)#EXTINF:([\d.]+)`)

// hlsMeta: 1 fetch playlist → judul asli (fn=), total byte (len=), durasi (#EXTINF).
func hlsMeta(hlsURL string) (fn string, size int64, durSec int64) {
	m3u8Text, err := fetchText(hlsURL)
	if err != nil {
		return "", 0, 0
	}
	var durSum float64
	var durSeen bool
	for _, line := range strings.Split(m3u8Text, "\n") {
		line = strings.TrimSpace(line)
		if strings.HasPrefix(line, "https://") {
			if fn == "" {
				fn = filenameFromSegment(line)
			}
			inner := innerSegmentURL(line)
			if p, err := url.Parse(inner); err == nil {
				q := p.Query()
				if ln := q.Get("len"); ln != "" {
					if strings.Contains(ln, "-") {
						var a, b int64
						if _, err := fmt.Sscanf(ln, "%d-%d", &a, &b); err == nil && b >= a {
							size += b - a + 1
						}
					} else if v, err := strconv.ParseInt(ln, 10, 64); err == nil {
						size += v
					}
				} else if rg := q.Get("range"); rg != "" {
					var a, b int64
					if _, err := fmt.Sscanf(rg, "%d-%d", &a, &b); err == nil && b >= a {
						size += b - a + 1
					}
				}
			}
			continue
		}
		if m := extinfRe.FindStringSubmatch(line); m != nil {
			if v, err := strconv.ParseFloat(m[1], 64); err == nil {
				durSum += v
				durSeen = true
			}
		}
	}
	if durSeen && durSum > 0 {
		durSec = int64(durSum + 0.5)
	}
	return fn, size, durSec
}

// ---------- helper HLS/segmen ----------

var segInnerRe = regexp.MustCompile(`(?i)^https?://[^/]*freeterabox\.com/video/`)

func fetchText(u string) (string, error) {
	req, _ := http.NewRequest("GET", u, nil)
	for k, v := range gidHeaders {
		req.Header.Set(k, v)
	}
	resp, err := scrapeHTTP.Do(req)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()
	b, err := io.ReadAll(io.LimitReader(resp.Body, 4<<20))
	return string(b), err
}

func innerSegmentURL(segment string) string {
	if segInnerRe.MatchString(segment) {
		return segment
	}
	p, err := url.Parse(segment)
	if err != nil {
		return segment
	}
	if inner := p.Query().Get("url"); inner != "" {
		return inner
	}
	return segment
}

func buildFullCDN(segment string) string {
	inner := innerSegmentURL(segment)
	p, err := url.Parse(inner)
	if err != nil {
		return ""
	}
	q := p.Query()
	ts := q.Get("ts_size")
	if ts == "" {
		return ""
	}
	tsN, err := strconv.Atoi(ts)
	if err != nil || tsN <= 0 {
		return ""
	}
	q.Del("len")
	q.Set("range", fmt.Sprintf("0-%d", tsN-1))
	p.RawQuery = q.Encode()
	return p.String()
}

func filenameFromSegment(segment string) string {
	inner := innerSegmentURL(segment)
	p, err := url.Parse(inner)
	if err != nil {
		return ""
	}
	return p.Query().Get("fn")
}

// ---------- entry ----------

func resolveTerabox(link string) ([]Item, error) {
	link = strings.TrimSpace(link)
	if !teraDomains.MatchString(link) {
		return nil, fmt.Errorf("URL bukan TeraBox valid (harus terabox.com / 1024terabox.com / teraboxapp.com / terasharelink.com)")
	}
	// Default: API "sendiri" (getindevice, native). Fallback: tbox-abf.
	if items, err := gidFallback(link); err == nil && len(items) > 0 {
		log.Printf("resolve terabox: %d item (getindevice)", len(items))
		return items, nil
	} else if err != nil {
		log.Printf("getindevice gagal: %v — fallback tbox", err)
	}
	items, err := fetchTbox(link)
	if err == nil {
		log.Printf("resolve terabox: %d item (tbox)", len(items))
	}
	return items, err
}
