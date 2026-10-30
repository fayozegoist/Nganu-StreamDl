package main

// SliceDrive / Videy / Vidmonstr resolver — port dari slicedrive.py.

import (
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"regexp"
	"strconv"
	"strings"
	"sync"
)

var videyCDNHosts = []string{
	"https://cdn2.videy.co/",
	"https://cdn.slicerdrive.online/",
	"https://box.vchod.cc/videos/",
	"https://cdn.aceimg.com/",
	"https://cdn2.aceimg.com/",
}

var (
	vCodeRes = []*regexp.Regexp{
		regexp.MustCompile(`(?i)slicedirev\.com/([A-Za-z0-9_-]+)`),
		regexp.MustCompile(`(?i)slicerdrive\.online/([A-Za-z0-9_-]+)`),
		regexp.MustCompile(`(?i)slicedrive\.com/([A-Za-z0-9_-]+)`),
		regexp.MustCompile(`(?i)vid3y\.my\.id/\?v=([A-Za-z0-9_-]+)`),
		regexp.MustCompile(`(?i)videy\.co/(?:v/)?([A-Za-z0-9_-]{5,}(?:\.mp4)?)`),
	}
	vFolderRe  = regexp.MustCompile(`(?i)vid(?:3\.de|monstr\.com)/f/([A-Za-z0-9]+)/?$`)
	vFileRe    = regexp.MustCompile(`(?i)vidmonstr\.com/d/([A-Za-z0-9]+)/?$`)
	vExtRe     = regexp.MustCompile(`(?i)\.(mp4|webm|mkv|avi|mov)$`)
	vHrefRe    = regexp.MustCompile(`(?i)href="/d/([a-z0-9]+)"`)
	vIframeRe  = regexp.MustCompile(`iframeId\s*=\s*'([^']+)'`)
	vTokenRe   = regexp.MustCompile(`embedToken\s*=\s*'([^']+)'`)
	vStreamRe  = regexp.MustCompile(`stream\.php[^"'\s]*`)
	vMP4Re     = regexp.MustCompile(`https://mp4-[\w.-]+\.overfetch\.video/[\w-]+`)
	vRangeRe   = regexp.MustCompile(`/(\d+)$`)
	vCodeEndRe = regexp.MustCompile(`/([A-Za-z0-9_-]{6,})$`)
	vCodeQRe   = regexp.MustCompile(`[?&]v=([A-Za-z0-9_-]+)`)
)

func videyGet(url string, headers map[string]string) (*http.Response, error) {
	req, _ := http.NewRequest("GET", url, nil)
	req.Header.Set("User-Agent", uaString)
	for k, v := range headers {
		req.Header.Set(k, v)
	}
	return doScrape(req)
}

func videyHead(url string) (*http.Response, error) {
	req, _ := http.NewRequest("HEAD", url, nil)
	req.Header.Set("User-Agent", uaString)
	return doScrape(req)
}

func resolveVidey(link string) ([]Item, error) {
	link = strings.TrimSpace(link)
	if link == "" {
		return nil, fmt.Errorf("link kosong")
	}

	// 1) file langsung
	if resp, err := videyHead(link); err == nil {
		ct := resp.Header.Get("Content-Type")
		resp.Body.Close()
		if resp.StatusCode < 400 && (strings.HasPrefix(ct, "video/") ||
			strings.HasPrefix(ct, "application/octet-stream") || vExtRe.MatchString(link)) {
			size, _ := strconv.ParseInt(resp.Header.Get("Content-Length"), 10, 64)
			title := strings.TrimSuffix(link[strings.LastIndex(link, "/")+1:], ".mp4")
			if title == "" {
				title = "video"
			}
			return []Item{{Title: title, URL: link, Stream: link, Size: size}}, nil
		}
	}

	// 2) ekstrak kode
	code := ""
	for _, re := range vCodeRes {
		if m := re.FindStringSubmatch(link); m != nil {
			code = m[1]
			break
		}
	}
	finalURL := link
	isCDN := strings.HasPrefix(strings.ToLower(strings.SplitN(strings.TrimPrefix(strings.TrimPrefix(link, "https://"), "http://"), "/", 2)[0]), "cdn.")
	if code == "" || isCDN {
		if resp, err := videyGet(link, map[string]string{"Accept": "*/*"}); err == nil {
			finalURL = resp.Request.URL.String()
			io.Copy(io.Discard, resp.Body)
			resp.Body.Close()
			if m := vCodeEndRe.FindStringSubmatch(finalURL); m != nil {
				code = m[1]
			} else if m := vCodeQRe.FindStringSubmatch(finalURL); m != nil {
				code = m[1]
			}
		}
	}
	if code == "" {
		return nil, fmt.Errorf("kode video tidak ditemukan pada URL")
	}

	// 3) vidmonstr folder / file
	if m := vFolderRe.FindStringSubmatch(finalURL); m != nil {
		return videyFolder(m[1])
	}
	if m := vFileRe.FindStringSubmatch(finalURL); m != nil {
		if it, err := videyFile(m[1], ""); err == nil {
			return []Item{*it}, nil
		}
		return nil, fmt.Errorf("gagal membaca halaman file vidmonstr")
	}

	// 4) probe CDN
	code = strings.TrimSuffix(code, ".mp4")
	var found string
	for _, host := range videyCDNHosts {
		cand := host + code + ".mp4"
		req, _ := http.NewRequest("GET", cand, nil)
		req.Header.Set("User-Agent", uaString)
		req.Header.Set("Range", "bytes=0-64")
		if resp, err := doScrape(req); err == nil {
			io.Copy(io.Discard, resp.Body)
			resp.Body.Close()
			if resp.StatusCode < 400 {
				found = cand
				break
			}
		}
	}
	if found == "" {
		return nil, fmt.Errorf("video tidak ditemukan di semua CDN")
	}
	size := headSize(found)
	title := strings.TrimSuffix(link[strings.LastIndex(link, "/")+1:], ".mp4")
	if title == "" {
		title = "video"
	}
	return []Item{{Title: title, URL: found, Stream: found, Size: size}}, nil
}

func headSize(url string) int64 {
	if resp, err := videyHead(url); err == nil {
		defer resp.Body.Close()
		if resp.StatusCode < 400 {
			if n, err2 := strconv.ParseInt(resp.Header.Get("Content-Length"), 10, 64); err2 == nil {
				return n
			}
		}
	}
	// fallback: Content-Range
	req, _ := http.NewRequest("GET", url, nil)
	req.Header.Set("User-Agent", uaString)
	req.Header.Set("Range", "bytes=0-0")
	if resp, err := doScrape(req); err == nil {
		defer resp.Body.Close()
		io.Copy(io.Discard, resp.Body)
		if m := vRangeRe.FindStringSubmatch(resp.Header.Get("Content-Range")); m != nil {
			if n, err2 := strconv.ParseInt(m[1], 10, 64); err2 == nil {
				return n
			}
		}
		if n, err2 := strconv.ParseInt(resp.Header.Get("Content-Length"), 10, 64); err2 == nil {
			return n
		}
	}
	return 0
}

func videyFolder(slug string) ([]Item, error) {
	resp, err := videyGet("https://vidmonstr.com/f/"+slug, map[string]string{"Accept": "*/*"})
	if err != nil || resp.StatusCode >= 400 {
		return nil, fmt.Errorf("gagal buka folder vidmonstr")
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(io.LimitReader(resp.Body, 4<<20))
	seen := map[string]bool{}
	var slugs []string
	for _, m := range vHrefRe.FindAllStringSubmatch(string(body), -1) {
		if !seen[m[1]] {
			seen[m[1]] = true
			slugs = append(slugs, m[1])
		}
	}
	if len(slugs) == 0 {
		return nil, fmt.Errorf("folder vidmonstr tidak berisi file video")
	}
	items := make([]Item, 0, len(slugs))
	var mu sync.Mutex
	var wg sync.WaitGroup
	sem := make(chan struct{}, 4)
	for i, s := range slugs {
		wg.Add(1)
		go func(i int, s string) {
			defer wg.Done()
			sem <- struct{}{}
			defer func() { <-sem }()
			if it, err := videyFile(s, strconv.Itoa(i+1)); err == nil {
				mu.Lock()
				items = append(items, *it)
				mu.Unlock()
			}
		}(i, s)
	}
	wg.Wait()
	if len(items) == 0 {
		return nil, fmt.Errorf("folder kosong / gagal dibaca")
	}
	return items, nil
}

func tokenTitle(token string) string {
	first := strings.SplitN(token, ".", 2)[0]
	pad := strings.Repeat("=", -len(first)%4)
	data, err := base64.URLEncoding.DecodeString(first + pad)
	if err != nil {
		// coba rawurl
		data, err = base64.RawURLEncoding.DecodeString(first)
		if err != nil {
			return ""
		}
	}
	var obj struct {
		RF string `json:"rf"`
	}
	if json.Unmarshal(data, &obj) != nil {
		return ""
	}
	return strings.TrimSuffix(obj.RF, ".mp4")
}

func videyFile(slug, name string) (*Item, error) {
	pageURL := "https://vidmonstr.com/d/" + slug
	resp, err := videyGet(pageURL, map[string]string{"Accept": "*/*"})
	if err != nil || resp.StatusCode >= 400 {
		return nil, fmt.Errorf("gagal buka %s", pageURL)
	}
	body, _ := io.ReadAll(io.LimitReader(resp.Body, 4<<20))
	resp.Body.Close()
	htmlText := string(body)
	im := vIframeRe.FindStringSubmatch(htmlText)
	tm := vTokenRe.FindStringSubmatch(htmlText)
	if im == nil || tm == nil {
		return nil, fmt.Errorf("token tidak ditemukan")
	}

	r2, err := videyGet("https://vidmonstr.com/ip129jk?id="+im[1]+"&t="+tm[1],
		map[string]string{"Referer": pageURL})
	if err != nil || r2.StatusCode >= 400 {
		return nil, fmt.Errorf("gagal ambil stream.php")
	}
	b2, _ := io.ReadAll(io.LimitReader(r2.Body, 4<<20))
	r2.Body.Close()
	sp := vStreamRe.FindString(string(b2))
	if sp == "" {
		return nil, fmt.Errorf("stream.php tidak ditemukan")
	}
	sp = strings.ReplaceAll(strings.ReplaceAll(sp, `\u0026`, "&"), "&amp;", "&")
	durl := "https://vidmonstr.com/" + sp

	r3, err := videyGet(durl, map[string]string{"Referer": pageURL})
	if err != nil || r3.StatusCode >= 400 {
		return nil, fmt.Errorf("gagal ambil halaman stream")
	}
	b3, _ := io.ReadAll(io.LimitReader(r3.Body, 8<<20))
	r3.Body.Close()

	title := tokenTitle(tm[1])
	if title == "" {
		if name != "" {
			title = name
		} else {
			title = slug
		}
	}
	title = strings.TrimSuffix(title, ".mp4")

	if m := vMP4Re.FindString(string(b3)); m != "" {
		size := headSizeRef(m, "https://vidmonstr.com/")
		return &Item{Title: title, URL: m, Stream: m, Size: size,
			Referer: "https://vidmonstr.com/"}, nil
	}
	return &Item{Title: title, URL: durl, Stream: durl, Referer: "https://vidmonstr.com/"}, nil
}

func headSizeRef(url, referer string) int64 {
	req, _ := http.NewRequest("HEAD", url, nil)
	req.Header.Set("User-Agent", uaString)
	if referer != "" {
		req.Header.Set("Referer", referer)
	}
	if resp, err := doScrape(req); err == nil {
		defer resp.Body.Close()
		if resp.StatusCode < 400 {
			if n, err2 := strconv.ParseInt(resp.Header.Get("Content-Length"), 10, 64); err2 == nil {
				return n
			}
		}
	}
	return 0
}
