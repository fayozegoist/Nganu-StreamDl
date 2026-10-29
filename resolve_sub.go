package main

// SubtitleCat search + fetch .srt — port dari bot.py (search_subtitle) dan subsrt.py.

import (
	"fmt"
	"html"
	"io"
	"net/http"
	"net/url"
	"regexp"
	"strings"
)

const subUA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36"

var (
	subTableRe = regexp.MustCompile(`(?is)<table[^>]*class="[^"]*\bsub-table\b[^"]*"[\s\S]*?</table>`)
	subRowRe   = regexp.MustCompile(`(?is)<tr\b[^>]*>([\s\S]*?)</tr>`)
	subCellRe  = regexp.MustCompile(`(?is)<td\b[^>]*>([\s\S]*?)</td>`)
	subARe     = regexp.MustCompile(`(?is)<a\b[^>]*href="([^"]+)"[^>]*>([\s\S]*?)</a>`)
	subTagRe   = regexp.MustCompile(`(?is)<[^>]+>`)
	subAnchRe  = regexp.MustCompile(`(?is)<a\b[\s\S]*?</a>`)
	subSRTRe   = regexp.MustCompile(`(?i)href="([^"]+\.srt)"`)
	subIDRe    = regexp.MustCompile(`(?i)-id\.srt$`)
	subENRe    = regexp.MustCompile(`(?i)-en\.srt$`)
	subParenRe = regexp.MustCompile(`^\(|\)$`)
)

func stripHTML(s string) string {
	s = subTagRe.ReplaceAllString(s, " ")
	return strings.Join(strings.Fields(html.UnescapeString(s)), " ")
}

func subGet(u string) (string, error) {
	req, _ := http.NewRequest("GET", u, nil)
	req.Header.Set("User-Agent", subUA)
	req.Header.Set("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8")
	resp, err := scrapeHTTP.Do(req)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 400 {
		return "", fmt.Errorf("HTTP %d", resp.StatusCode)
	}
	b, err := io.ReadAll(io.LimitReader(resp.Body, 8<<20))
	return string(b), err
}

type subResult struct {
	Title, DetailURL, Size, Downloads string
}

func searchSubtitle(query string) ([]Item, error) {
	searchURL := "https://www.subtitlecat.com/index.php?search=" + url.QueryEscape(query)
	htmlText, err := subGet(searchURL)
	if err != nil {
		return nil, err
	}
	table := htmlText
	if m := subTableRe.FindString(htmlText); m != "" {
		table = m
	}
	var items []Item
	for _, row := range subRowRe.FindAllStringSubmatch(table, -1) {
		tds := subCellRe.FindAllStringSubmatch(row[1], -1)
		if len(tds) == 0 {
			continue
		}
		a := subARe.FindStringSubmatch(tds[0][1])
		if a == nil {
			continue
		}
		title := stripHTML(a[2])
		if title == "" {
			continue
		}
		detail, err := url.Parse(a[1])
		if err != nil {
			continue
		}
		base, _ := url.Parse("https://www.subtitlecat.com")
		size := ""
		if len(tds) > 2 {
			size = stripHTML(tds[2][1])
		}
		dl := ""
		if len(tds) > 3 {
			dl = stripHTML(tds[3][1])
		}
		items = append(items, Item{
			Title:     title,
			SizeLabel: fmt.Sprintf("%s | %s unduhan", size, dl),
			Detail:    base.ResolveReference(detail).String(),
		})
	}
	return items, nil
}

func fetchSubtitleSRT(detailURL string) ([]byte, string, error) {
	htmlText, err := subGet(detailURL)
	if err != nil {
		return nil, "", err
	}
	var links []string
	for _, m := range subSRTRe.FindAllStringSubmatch(htmlText, -1) {
		links = append(links, m[1])
	}
	if len(links) == 0 {
		return nil, "", fmt.Errorf("link SRT tidak ditemukan")
	}
	pick := links[0]
	for _, pref := range []*regexp.Regexp{subIDRe, subENRe} {
		for _, l := range links {
			if pref.MatchString(l) {
				pick = l
				break
			}
		}
	}
	srtURL := detailURL
	if u, err := url.Parse(pick); err == nil {
		base, _ := url.Parse(detailURL)
		srtURL = base.ResolveReference(u).String()
	}
	resp, err := scrapeHTTP.Get(srtURL)
	if err != nil {
		return nil, "", err
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 400 {
		return nil, "", fmt.Errorf("HTTP %d", resp.StatusCode)
	}
	data, err := io.ReadAll(io.LimitReader(resp.Body, 4<<20))
	if err != nil || len(strings.TrimSpace(string(data))) == 0 {
		return nil, "", fmt.Errorf("SRT kosong")
	}
	fname := pick
	if i := strings.LastIndex(pick, "/"); i >= 0 {
		fname = pick[i+1:]
	}
	fname, _ = url.QueryUnescape(fname)
	if fname == "" {
		fname = "subtitle.srt"
	}
	return data, fname, nil
}
