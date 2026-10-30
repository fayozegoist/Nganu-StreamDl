package main

// UC Share (uc-share.com) resolver — port dari ucshare.py.

import (
	"bytes"
	"encoding/json"
	"fmt"
	"net/http"
	"regexp"
	"strings"
	"sync"
)

const ucBase = "https://m-intldrive.ucweb.com/1/clouddrive/share/sharepage"
const ucOrigin = "https://uc-share.com"
const uaString = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"

func ucAPI(path string, payload map[string]any, headers map[string]string) (map[string]any, error) {
	body, _ := json.Marshal(payload)
	req, _ := http.NewRequest("POST", ucBase+"/"+path, bytes.NewReader(body))
	req.Header.Set("User-Agent", uaString)
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Origin", ucOrigin)
	req.Header.Set("Referer", ucOrigin+"/")
	req.Header.Set("Accept", "application/json, text/plain, */*")
	for k, v := range headers {
		req.Header.Set(k, v)
	}
	resp, err := doScrape(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	var data map[string]any
	if err := json.NewDecoder(resp.Body).Decode(&data); err != nil {
		return nil, err
	}
	code, _ := data["code"].(float64)
	if code != 0 {
		msg, _ := data["message"].(string)
		if msg == "" {
			msg = fmt.Sprintf("HTTP %d", resp.StatusCode)
		}
		return nil, fmt.Errorf("UC request gagal: %s", msg)
	}
	return data, nil
}

var ucPwdRe1 = regexp.MustCompile(`(?i)uc-share\.com/s/([a-z0-9]+)`)
var ucPwdRe2 = regexp.MustCompile(`(?i)[?&]s=([a-z0-9]+)`)
var ucPassRe = regexp.MustCompile(`(?i)[?&](?:pwd|passcode|pass)=([^&]+)`)

type ucFile struct {
	FID           string `json:"fid"`
	Dir           bool   `json:"dir"`
	FileName      string `json:"file_name"`
	Size          int64  `json:"size"`
	ShareFIDToken string `json:"share_fid_token"`
}

func resolveUC(link string) ([]Item, error) {
	m := ucPwdRe1.FindStringSubmatch(link)
	if m == nil {
		m = ucPwdRe2.FindStringSubmatch(link)
	}
	if m == nil {
		return nil, fmt.Errorf("pwd_id UC Share tidak ditemukan pada URL")
	}
	pwdID := m[1]
	passcode := ""
	if pm := ucPassRe.FindStringSubmatch(link); pm != nil {
		passcode = pm[1]
	}

	detail, err := ucAPI("v2/detail", map[string]any{
		"pwd_id": pwdID, "passcode": passcode, "force": 0, "page": 1, "size": 50,
		"fetch_banner": 1, "fetch_share": 1, "fetch_total": 1, "sort": "",
		"banner_platform": "pc", "fetch_error_background": 1, "web_platform": "pc",
		"fetch_follow_status": 1, "ip_limit": 0,
	}, nil)
	if err != nil {
		return nil, err
	}
	dd, _ := detail["data"].(map[string]any)
	tok, _ := dd["token_info"].(map[string]any)
	stoken, _ := tok["stoken"].(string)
	if stoken == "" {
		return nil, fmt.Errorf("stoken UC Share tidak didapatkan")
	}

	var files []ucFile
	seen := map[string]bool{}
	queue := []string{"0"}
	for len(queue) > 0 {
		fid := queue[0]
		queue = queue[1:]
		d, err := ucAPI("v2/detail", map[string]any{
			"pwd_id": pwdID, "passcode": passcode, "stoken": stoken, "pdir_fid": fid,
			"force": 0, "_page": 1, "_size": 50, "_fetch_total": 1, "_sort": "",
			"ip_limit": 0, "web_platform": "pc",
		}, nil)
		if err != nil {
			continue
		}
		dd, _ := d["data"].(map[string]any)
		di, _ := dd["detail_info"].(map[string]any)
		raw, _ := di["list"].([]any)
		if len(raw) == 0 && fid == "0" {
			break
		}
		for _, v := range raw {
			b, _ := json.Marshal(v)
			var f ucFile
			if json.Unmarshal(b, &f) != nil {
				continue
			}
			if f.Dir {
				if f.FID != "" && !seen[f.FID] {
					seen[f.FID] = true
					queue = append(queue, f.FID)
				}
			} else {
				files = append(files, f)
			}
		}
	}
	if len(files) == 0 {
		return nil, fmt.Errorf("share kosong / sudah expired")
	}

	items := make([]Item, len(files))
	var wg sync.WaitGroup
	sem := make(chan struct{}, 3)
	for i, f := range files {
		wg.Add(1)
		go func(i int, f ucFile) {
			defer wg.Done()
			sem <- struct{}{}
			defer func() { <-sem }()
			items[i] = ucMedia(pwdID, stoken, f)
		}(i, f)
	}
	wg.Wait()
	return items, nil
}

func ucMedia(pwdID, stoken string, f ucFile) Item {
	q := "pwd_id=" + pwdID + "&fid=" + f.FID +
		"&fid_token=" + f.ShareFIDToken + "&stoken=" + stoken
	req, _ := http.NewRequest("GET", ucBase+"/video_preview?"+q, nil)
	req.Header.Set("User-Agent", uaString)
	req.Header.Set("x-clouddrive-st", stoken)
	req.Header.Set("Referer", ucOrigin+"/")

	it := Item{Title: f.FileName, Size: f.Size}
	resp, err := doScrape(req)
	if err != nil {
		return it
	}
	defer resp.Body.Close()
	var r struct {
		Data struct {
			PlayInfo struct {
				URL      string  `json:"url"`
				Size     int64   `json:"size"`
				Width    int     `json:"width"`
				Height   int     `json:"height"`
				Duration float64 `json:"duration"`
			} `json:"play_info"`
		} `json:"data"`
	}
	ct := resp.Header.Get("Content-Type")
	if !strings.HasPrefix(ct, "application/json") {
		return it
	}
	if json.NewDecoder(resp.Body).Decode(&r) != nil {
		return it
	}
	if r.Data.PlayInfo.URL == "" {
		return it
	}
	it.URL = r.Data.PlayInfo.URL
	it.Stream = it.URL
	if r.Data.PlayInfo.Size > 0 {
		it.Size = r.Data.PlayInfo.Size
	}
	if r.Data.PlayInfo.Duration > 0 {
		it.Duration = fmtDur(int64(r.Data.PlayInfo.Duration))
	}
	if strings.Contains(it.URL, "aliyuncs.com") {
		it.Referer = ucOrigin + "/"
	}
	return it
}
