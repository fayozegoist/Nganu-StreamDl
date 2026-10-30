package main

// Nganu-StreamDl — versi Go. Command: /tera /videy /uc /subtitle /stop /cancel /infovps.
// Daftar hasil: text link + pagination inline button (edit pesan yang sama).

import (
	"bytes"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"html"
	"log"
	"net/http"
	"os"
	"os/exec"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"time"
)

var (
	ownerID        = envInt64("OWNER_ID", 0)
	channelID      = envInt64("CHANNEL_ID", 0)
	uploaderURL    = envStr("UPLOADER_URL", "http://127.0.0.1:8399")
	maxUploadBytes = int64(envInt("MAX_UPLOAD_GB", 2)) << 30
	// plafon protokol MTProto: 4000 part x 512KB — berlaku untuk SEMUA bot
	maxTelegramFile = int64(4000) * 512 * 1024 // 2_048_000_000 ≈ 1.91 GiB
	teraCooldown    = 60 * time.Second
	sessionTTL      = 10 * time.Minute
	botUsername     string
)

func envStr(k, def string) string {
	if v := strings.TrimSpace(os.Getenv(k)); v != "" {
		return v
	}
	return def
}

func envInt(k string, def int) int {
	if v, err := strconv.Atoi(strings.TrimSpace(os.Getenv(k))); err == nil {
		return v
	}
	return def
}

func envInt64(k string, def int64) int64 {
	if v, err := strconv.ParseInt(strings.TrimSpace(os.Getenv(k)), 10, 64); err == nil {
		return v
	}
	return def
}

// ---------------- state ----------------

type Item struct {
	Title     string
	Size      int64 // byte; 0 = tidak diketahui
	SizeLabel string
	Duration  string // "m:ss" / "h:mm:ss"; kosong bila tak diketahui
	URL       string // link download langsung
	Stream    string
	Thumb     string // url thumbnail (opsional)
	Detail    string // subtitle: url halaman detail
	// terabox fallback (getindevice): isi salah satu
	HLS     string
	Direct  string
	Referer string
}

type Session struct {
	Platform string
	Chat     int64
	Items    []Item
	Expires  time.Time
}

type state struct {
	mu      sync.Mutex
	sess    map[string]*Session // token acak per daftar — link lama tak tertimpa
	pending map[int64]string
	teraCD  map[int64]time.Time
	jobs    map[int64]map[string]bool // chatID → jobID upload aktif (untuk /stop)
}

var st = &state{
	sess:    map[string]*Session{},
	pending: map[int64]string{},
	teraCD:  map[int64]time.Time{},
	jobs:    map[int64]map[string]bool{},
}

func (s *state) setSession(chatID int64, sess *Session) string {
	s.mu.Lock()
	defer s.mu.Unlock()
	sess.Expires = time.Now().Add(sessionTTL)
	sess.Chat = chatID
	token := randToken()
	s.sess[token] = sess
	// sapu sesi kedaluwarsa
	for t, v := range s.sess {
		if time.Now().After(v.Expires) {
			delete(s.sess, t)
		}
	}
	return token
}

func (s *state) getSession(token string) *Session {
	s.mu.Lock()
	defer s.mu.Unlock()
	sess := s.sess[token]
	if sess == nil || time.Now().After(sess.Expires) {
		delete(s.sess, token)
		return nil
	}
	return sess
}

func (s *state) setPending(chatID int64, platform string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.pending[chatID] = platform
}

func (s *state) takePending(chatID int64) (string, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	p, ok := s.pending[chatID]
	delete(s.pending, chatID)
	return p, ok
}

func (s *state) clear(chatID int64) {
	s.mu.Lock()
	defer s.mu.Unlock()
	delete(s.pending, chatID)
}

func (s *state) cooldownOK(chatID int64) (time.Duration, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	last, ok := s.teraCD[chatID]
	if !ok || time.Since(last) >= teraCooldown {
		return 0, true
	}
	return teraCooldown - time.Since(last), false
}

func (s *state) setCooldown(chatID int64) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.teraCD[chatID] = time.Now()
}

func (s *state) addJob(chatID int64, jobID string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	set := s.jobs[chatID]
	if set == nil {
		set = map[string]bool{}
		s.jobs[chatID] = set
	}
	set[jobID] = true
}

func (s *state) removeJob(chatID int64, jobID string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if set := s.jobs[chatID]; set != nil {
		delete(set, jobID)
		if len(set) == 0 {
			delete(s.jobs, chatID)
		}
	}
}

func (s *state) activeJobs(chatID int64) []string {
	s.mu.Lock()
	defer s.mu.Unlock()
	var out []string
	for j := range s.jobs[chatID] {
		out = append(out, j)
	}
	return out
}

// ---------------- helpers ----------------

func randToken() string {
	b := make([]byte, 4)
	if _, err := rand.Read(b); err != nil {
		return strconv.FormatInt(time.Now().UnixNano(), 36)
	}
	return hex.EncodeToString(b)
}

// ---------------- premium emoji ----------------

const (
	emoWelcome   = "5767035596895688395"
	emoTerabox   = "6179473914691848390"
	emoUC        = "5325651836634413305"
	emoVidey     = "5983495398788962050"
	emoExtracted = "6269225223506109608"
	emoTotal     = "6269476912884621728"
	emoTitle     = "5321321693556261256"
	emoSize      = "5470016867252855402"
	emoDuration  = "5445350406215465190"
	emoDownload  = "5255741073409452371"
	emoStreaming = "5445158077579952110"
	emoPhoto     = "5257974976094412956"
	emoUpload    = "5911100572508885928"
	emoDone      = "6217663806110175239"
	emoSpeed     = "5042334757040423886"
	emoProcess   = "5350427505805238170"
)

func emo(id, fallback string) string {
	return `<tg-emoji emoji-id="` + id + `">` + fallback + `</tg-emoji>`
}

func esc(s string) string { return html.EscapeString(s) }

func fmtSize(n int64) string {
	if n <= 0 {
		return "-"
	}
	units := []string{"B", "KB", "MB", "GB", "TB"}
	v := float64(n)
	i := 0
	for v >= 1024 && i < len(units)-1 {
		v /= 1024
		i++
	}
	return fmt.Sprintf("%.0f %s", v, units[i])
}

func shortTitle(s string, max int) string {
	s = strings.Join(strings.Fields(s), " ")
	r := []rune(s)
	if len(r) <= max {
		return s
	}
	return string(r[:max]) + "…"
}

// parseDeepLink: "dl-a1b2c3d4-3" atau "sub-a1b2c3d4-2" → kind, token, n.
func parseDeepLink(arg string) (kind, token string, n int, ok bool) {
	parts := strings.Split(arg, "-")
	if len(parts) != 3 || (parts[0] != "dl" && parts[0] != "sub") {
		return "", "", 0, false
	}
	nv, err := strconv.Atoi(parts[2])
	if err != nil {
		return "", "", 0, false
	}
	return parts[0], parts[1], nv, true
}

func handleDeepLink(chat int64, kind, token string, n int) {
	sess := st.getSession(token)
	if sess == nil || n < 1 || n > len(sess.Items) {
		sendHTML(chat, "❌ Link download kedaluwarsa. Ketik /tera (atau command lain) lagi.")
		return
	}
	it := sess.Items[n-1]
	target := sess.Chat
	if kind == "sub" {
		go func() {
			data, fname, err := fetchSubtitleSRT(it.Detail)
			if err != nil {
				sendHTML(target, "❌ "+esc(err.Error()))
				return
			}
			if err := sendDoc(target, data, fname, it.Title); err != nil {
				sendHTML(target, "❌ Gagal kirim subtitle: "+esc(err.Error()))
			}
		}()
		return
	}
	go uploadAndSend(target, n, it)
}

var platformHints = map[string]string{
	"terabox":    emo(emoTerabox, "📦") + " Kirim link TeraBox:",
	"slicedrive": emo(emoVidey, "🎞") + " Kirim link Videy:",
	"ucshare":    emo(emoUC, "☁️") + " Kirim link UC Share:",
	"subtitle":   "Kirim kode nuklir:",
}

var platformCmd = map[string]string{
	"/tera":     "terabox",
	"/videy":    "slicedrive",
	"/uc":       "ucshare",
	"/subtitle": "subtitle",
}

var startTextNew = fmt.Sprintf(`%s <b>Selamat datang!</b>

Aku bot downloader video & subtitle — unduh & unggah paralel super cepat.

Command:

<b>/tera</b> &lt;link terabox&gt;
<b>/videy</b> &lt;link videy&gt;
<b>/uc</b> &lt;link uc share&gt;
<b>/subtitle</b> &lt;kode nuklir&gt;

Kirim link, lalu klik %s <b>Download</b> di file yang kamu mau.`,
	emo(emoWelcome, "👋"), emo(emoDownload, "⬇️"))

var startTextOld = fmt.Sprintf(`%s <b>Selamat kembali!</b>

Langsung kirim: /tera • /videy • /uc • /subtitle`,
	emo(emoWelcome, "👋"))

// ---------------- users (pembeda /start baru vs lama) ----------------

var (
	users   = map[int64]bool{}
	usersMu sync.Mutex
)

func usersLoad() {
	data, err := os.ReadFile("users.json")
	if err == nil {
		json.Unmarshal(data, &users)
	}
}

func usersKnown(uid int64) bool {
	usersMu.Lock()
	defer usersMu.Unlock()
	return users[uid]
}

func usersAdd(uid int64) {
	usersMu.Lock()
	defer usersMu.Unlock()
	if users[uid] {
		return
	}
	users[uid] = true
	if data, err := json.Marshal(users); err == nil {
		os.WriteFile("users.json", data, 0600)
	}
}

var imgExtRe = regexp.MustCompile(`(?i)\.(jpe?g|png|webp|gif|bmp)(\?|$)`)

func isImageItem(it Item) bool {
	return imgExtRe.MatchString(it.Title) || imgExtRe.MatchString(it.URL)
}

const perPage = 7

func renderItems(platform, token string, items []Item, page int) string {
	var b strings.Builder
	pages := (len(items) + perPage - 1) / perPage
	fmt.Fprintf(&b, "%s <b>Berhasil di ekstrak</b>\n\n%s Total: <b>%d</b>",
		emo(emoExtracted, "✅"), emo(emoTotal, "📊"), len(items))
	if pages > 1 {
		fmt.Fprintf(&b, " • Halaman %d/%d", page+1, pages)
	}
	b.WriteByte('\n')
	start := page * perPage
	end := start + perPage
	if end > len(items) {
		end = len(items)
	}
	for i := start; i < end; i++ {
		it := items[i]
		size := it.SizeLabel
		if size == "" {
			size = fmtSize(it.Size)
		}
		dur := it.Duration
		if dur == "" {
			dur = "-"
		}
		fmt.Fprintf(&b, "\n<b>%d.</b> %s %s\n　　%s Size: %s\n　　%s Durasi: %s\n",
			i+1, emo(emoTitle, "🎬"), esc(shortTitle(it.Title, 60)),
			emo(emoSize, "💾"), esc(size),
			emo(emoDuration, "⏱"), esc(dur))
		kind := "dl"
		if platform == "subtitle" {
			kind = "sub"
		}
		deep := fmt.Sprintf("https://t.me/%s?start=%s-%s-%d", botUsername, kind, token, i+1)
		label := "Download"
		if kind == "sub" {
			label = "Download .srt"
		}
		fmt.Fprintf(&b, "　　%s %s: <a href=\"%s\">klik disini</a>\n",
			emo(emoDownload, "⬇️"), label, deep)
		if it.Stream != "" {
			if isImageItem(it) {
				fmt.Fprintf(&b, "　　%s Buka Photo: <a href=\"%s\">klik disini</a>\n",
					emo(emoPhoto, "🖼"), esc(it.Stream))
			} else {
				fmt.Fprintf(&b, "　　%s Streaming: <a href=\"%s\">klik disini</a>\n",
					emo(emoStreaming, "▶️"), esc(it.Stream))
			}
		}
	}
	return b.String()
}

// navMarkup: tombol ⬅️ ➡️ untuk daftar > perPage item.
func navMarkup(token string, page, pages int) map[string]any {
	if pages <= 1 {
		return nil
	}
	row := []tbtn{}
	if page > 0 {
		row = append(row, tbtn{"⬅️ Sebelumnya", fmt.Sprintf("pg:%s:%d", token, page-1)})
	}
	row = append(row, tbtn{fmt.Sprintf("%d/%d", page+1, pages), "pg:noop"})
	if page+1 < pages {
		row = append(row, tbtn{"Selanjutnya ➡️", fmt.Sprintf("pg:%s:%d", token, page+1)})
	}
	return map[string]any{"inline_keyboard": [][]tbtn{row}}
}

func handleCallback(cq *CallbackQuery) {
	if cq.Message == nil || cq.From == nil {
		return
	}
	log.Printf("cb uid=%d data=%q", cq.From.ID, cq.Data)
	if cq.Data == "pg:noop" || !strings.HasPrefix(cq.Data, "pg:") {
		answerCallback(cq.ID, "", false)
		return
	}
	parts := strings.SplitN(cq.Data, ":", 3)
	page, err := strconv.Atoi(parts[len(parts)-1])
	sess := st.getSession(parts[1])
	if len(parts) != 3 || err != nil || sess == nil {
		answerCallback(cq.ID, "Sesi kedaluwarsa — kirim command lagi.", true)
		return
	}
	pages := (len(sess.Items) + perPage - 1) / perPage
	if page < 0 || page >= pages {
		answerCallback(cq.ID, "", false)
		return
	}
	text := renderItems(sess.Platform, parts[1], sess.Items, page)
	if err := editHTML(cq.Message.Chat.ID, cq.Message.MessageID, text,
		navMarkup(parts[1], page, pages)); err != nil {
		log.Printf("edit paging: %v", err)
	}
	answerCallback(cq.ID, "", false)
}

// ---------------- handlers ----------------

func handle(m *Message) {
	text := strings.TrimSpace(m.Text)
	if text == "" || m.From == nil {
		return
	}
	chat := m.Chat.ID
	uid := m.From.ID

	cmd := strings.ToLower(strings.Fields(text)[0])
	if i := strings.Index(cmd, "@"); i > 0 { // /cmd@botname
		cmd = cmd[:i]
	}
	log.Printf("msg uid=%d cmd=%q", uid, cmd)
	arg := strings.TrimSpace(strings.TrimPrefix(text, strings.Fields(text)[0]))

	switch cmd {
	case "/start", "/help", "/menu", "/id":
		st.clear(chat)
		if cmd == "/id" {
			sendHTML(chat, fmt.Sprintf("ID: <code>%d</code>", uid))
			return
		}
		if cmd == "/start" {
			if k, tok, n, ok := parseDeepLink(arg); ok {
				usersAdd(uid)
				handleDeepLink(chat, k, tok, n)
				return
			}
		}
		if usersKnown(uid) {
			sendHTML(chat, startTextOld)
		} else {
			usersAdd(uid)
			sendHTML(chat, startTextNew)
		}
		return
	case "/cancel":
		st.clear(chat)
		if jobs := st.activeJobs(chat); len(jobs) > 0 {
			for _, j := range jobs {
				cancelUpload(j)
			}
			sendHTML(chat, "🚫 Membatalkan "+strconv.Itoa(len(jobs))+" upload...")
		} else {
			sendHTML(chat, "✅ Dibatalkan.")
		}
		return
	case "/stop":
		jobs := st.activeJobs(chat)
		if len(jobs) == 0 {
			sendHTML(chat, "Tidak ada upload aktif.")
			return
		}
		for _, j := range jobs {
			cancelUpload(j)
		}
		sendHTML(chat, "🚫 Membatalkan "+strconv.Itoa(len(jobs))+" upload...")
		return
	case "/proxy":
		if uid != ownerID {
			sendHTML(chat, "⛔ Khusus admin.")
			return
		}
		handleProxyCmd(chat, arg)
		return
	case "/infovps":
		if uid != ownerID {
			sendHTML(chat, "⛔ Khusus admin.")
			return
		}
		sendHTML(chat, vpsInfo())
		return
	}

	platform, ok := platformCmd[cmd]
	if !ok {
		if p, ok := st.takePending(chat); ok {
			platform, arg = p, text
		} else {
			sendHTML(chat, "Perintah tidak dikenal. /start untuk bantuan.")
			return
		}
	}
	if arg == "" {
		st.setPending(chat, platform)
		sendHTML(chat, platformHints[platform])
		return
	}
	if platform == "terabox" && uid != ownerID {
		if remain, ok := st.cooldownOK(chat); !ok {
			sendHTML(chat, fmt.Sprintf("⏳ Jeda 60 detik... tunggu %d detik lagi.", int(remain.Seconds())+1))
			return
		}
	}
	go process(chat, uid, platform, arg)
}

func process(chat, uid int64, platform, payload string) {
	proc, err := sendHTML(chat, emo(emoProcess, "⏳")+" Processing...")
	if err != nil {
		return
	}
	fail := func(err error) {
		editHTML(chat, proc.MessageID, "❌ "+esc(shortTitle(err.Error(), 300)))
	}

	var items []Item
	switch platform {
	case "terabox":
		items, err = resolveTerabox(payload)
		if err != nil { // tbox gratisan flaky — coba sekali lagi
			log.Printf("resolve terabox error: %v (retry)", err)
			time.Sleep(2 * time.Second)
			items, err = resolveTerabox(payload)
		}
	case "slicedrive":
		items, err = resolveVidey(payload)
	case "ucshare":
		items, err = resolveUC(payload)
	case "subtitle":
		items, err = searchSubtitle(payload)
	}
	if err != nil {
		log.Printf("resolve %s gagal: %v", platform, err)
		fail(err)
		return
	}
	if platform != "terabox" { // terabox sudah dilog resolveTerabox (sumber platform)
		log.Printf("resolve %s: %d item", platform, len(items))
	}
	if len(items) == 0 {
		if platform == "terabox" {
			fail(fmt.Errorf("tidak ada file ditemukan — kalau ini share folder, resolver cadangan tak bisa masuk folder; coba lagi sebentar / laporkan ke admin"))
		} else {
			fail(fmt.Errorf("tidak ada file ditemukan"))
		}
		return
	}
	token := st.setSession(chat, &Session{Platform: platform, Items: items})
	pages := (len(items) + perPage - 1) / perPage
	text := renderItems(platform, token, items, 0)
	if err := editHTML(chat, proc.MessageID, text, navMarkup(token, 0, pages)); err != nil {
		sendHTML(chat, text, navMarkup(token, 0, pages))
	}
	if platform == "terabox" && uid != ownerID {
		st.setCooldown(chat)
	}
}

func uploadAndSend(chat int64, n int, it Item) {
	dl := it.URL
	if dl == "" && it.HLS == "" && it.Direct != "" {
		dl = it.Direct
	}
	if dl == "" && it.HLS == "" {
		sendHTML(chat, "❌ Item ini tidak punya link download.")
		return
	}
	limit := maxUploadBytes
	if limit > maxTelegramFile {
		limit = maxTelegramFile
	}
	manual := dl
	if manual == "" {
		manual = it.HLS
	}
	if it.Size > limit {
		sendHTML(chat, fmt.Sprintf(
			"⚠️ File <b>%s</b> melebihi batas upload (%s)\n%s Download manual lewat browser:\n%s",
			esc(fmtSize(it.Size)), fmtSize(limit), emo(emoDownload, "⬇️"), manual))
		return
	}
	proc, _ := sendHTML(chat, fmt.Sprintf("%s #%d %s — mengunduh...\nKetik /stop untuk membatalkan",
		emo(emoProcess, "⏳"), n, esc(shortTitle(it.Title, 40))))
	fname := it.Title
	if fname == "" {
		fname = "video.mp4"
	}
	jobID := randToken()
	st.addJob(chat, jobID)
	defer st.removeJob(chat, jobID)
	quit := make(chan struct{})
	go pollProgress(chat, proc.MessageID, jobID, quit)
	res, err := uploaderSend(chat, dl, it.HLS, fname, it.Referer, it.Thumb, jobID, limit)
	close(quit)
	if err != nil {
		if strings.Contains(err.Error(), "dibatalkan") {
			editHTML(chat, proc.MessageID, "🚫 Upload dibatalkan.")
			return
		}
		editHTML(chat, proc.MessageID, "❌ Upload gagal: "+esc(shortTitle(err.Error(), 250))+"\n\n⬇️ "+manual)
		return
	}
	spdDl := fmtSize(int64(float64(res.Size) / max(res.Tdl, 0.1)))
	spdUp := fmtSize(int64(float64(res.Size) / max(res.Tup, 0.1)))
	editHTML(chat, proc.MessageID, fmt.Sprintf(
		"%s <b>Selesai</b> — %s\n%s Download: %s • %s %s/s\n%s Upload: %s • %s %s/s",
		emo(emoDone, "✅"), esc(fmtSize(res.Size)),
		emo(emoDownload, "⬇️"), fmtSecs(res.Tdl), emo(emoSpeed, "⚡"), spdDl,
		emo(emoUpload, "⬆️"), fmtSecs(res.Tup), emo(emoSpeed, "⚡"), spdUp))
}

var upProgHTTP = &http.Client{Timeout: 5 * time.Second}

var cancelHTTP = &http.Client{Timeout: 5 * time.Second}

// cancelUpload meminta uploader menghentikan job — fire and forget.
func cancelUpload(jobID string) {
	resp, err := cancelHTTP.Get(uploaderURL + "/cancel?job_id=" + jobID)
	if err == nil {
		resp.Body.Close()
	}
}

// pollProgress menyunting pesan status tiap 4 detik: speed download & % upload.
func pollProgress(chat int64, msgID int, jobID string, quit chan struct{}) {
	var last string
	for {
		select {
		case <-quit:
			return
		case <-time.After(4 * time.Second):
		}
		resp, err := upProgHTTP.Get(uploaderURL + "/progress?job_id=" + jobID)
		if err != nil {
			continue
		}
		var p struct {
			Phase   string  `json:"phase"`
			Done    int64   `json:"done"`
			Total   int64   `json:"total"`
			Started float64 `json:"started"`
		}
		json.NewDecoder(resp.Body).Decode(&p)
		resp.Body.Close()
		if p.Phase != "download" && p.Phase != "upload" {
			continue
		}
		elapsed := time.Since(time.Unix(int64(p.Started), 0)).Seconds()
		if elapsed < 0.5 {
			elapsed = 0.5
		}
		speed := fmtSize(int64(float64(p.Done) / elapsed))
		icon := emo(emoDownload, "⬇️") + " Mengunduh…"
		if p.Phase == "upload" {
			icon = emo(emoUpload, "⬆️") + " Mengupload…"
		}
		var line string
		if p.Total > 0 {
			line = fmt.Sprintf("%s %d%% (%s/%s • %s%s/s)", icon,
				p.Done*100/max(p.Total, 1), fmtSize(p.Done), fmtSize(p.Total),
				emo(emoSpeed, "⚡"), speed)
		} else {
			line = fmt.Sprintf("%s %s • %s%s/s", icon, fmtSize(p.Done),
				emo(emoSpeed, "⚡"), speed)
		}
		line += "\nKetik /stop untuk membatalkan"
		if line == last {
			continue
		}
		last = line
		editHTML(chat, msgID, line)
	}
}

var upHTTP = &http.Client{Timeout: 35 * time.Minute}

// uploaderSend memanggil uploader.py (bot MTProto) lokal.
type upResult struct {
	OK        bool    `json:"ok"`
	MessageID int     `json:"message_id"`
	Error     string  `json:"error"`
	Tdl       float64 `json:"t_dl"`
	Tup       float64 `json:"t_up"`
	Size      int64   `json:"size"`
}

func fmtSecs(t float64) string {
	if t < 60 {
		return fmt.Sprintf("%.0fs", t)
	}
	return fmt.Sprintf("%dm%02ds", int(t)/60, int(t)%60)
}

func uploaderSend(chatID int64, url, hlsURL, filename, referer, thumbURL, jobID string, maxBytes int64) (*upResult, error) {
	payload := map[string]any{
		"chat_id":   chatID,
		"url":       url,
		"hls_url":   hlsURL,
		"filename":  filename,
		"caption":   filename,
		"referer":   referer,
		"thumb_url": thumbURL,
		"max_bytes": maxBytes,
		"job_id":    jobID,
	}
	body, _ := json.Marshal(payload)
	resp, err := upHTTP.Post(uploaderURL+"/send", "application/json", bytes.NewReader(body))
	if err != nil {
		return nil, fmt.Errorf("uploader offline: %w", err)
	}
	defer resp.Body.Close()
	var r upResult
	if err := json.NewDecoder(resp.Body).Decode(&r); err != nil {
		return nil, err
	}
	if !r.OK {
		return nil, fmt.Errorf("%s", r.Error)
	}
	return &r, nil
}

func getBotUsername() string {
	raw, err := tgCall("getMe", map[string]any{})
	if err != nil {
		return ""
	}
	var m struct {
		Username string `json:"username"`
	}
	json.Unmarshal(raw, &m)
	return m.Username
}

// ---------------- uploader subprocess ----------------

// startUploader menjalankan uploader.py sebagai anak proses dan restart
// otomatis dengan backoff. Platform hanya mengelola 1 service (binary Go),
// jadi uploader MTProto ikut di dalamnya.
func startUploader() {
	// ponytail: bootstrap pip+telethon sekali; cache di $HOME user pod
	bootstrap := func() bool {
		if err := exec.Command("python3", "-c", "import telethon, requests, cryptg").Run(); err == nil {
			return true
		}
		log.Println("bootstrap: install pip + telethon + requests")
		cmd := exec.Command("sh", "-c",
			"curl -fsSL https://bootstrap.pypa.io/get-pip.py -o /tmp/get-pip.py "+
				"&& python3 /tmp/get-pip.py --break-system-packages "+
				"&& python3 -m pip install --break-system-packages telethon cryptg requests hachoir")
		cmd.Stdout, cmd.Stderr = os.Stdout, os.Stderr
		if err := cmd.Run(); err != nil {
			log.Printf("bootstrap gagal: %v", err)
			return false
		}
		// ffmpeg static untuk thumbnail videy/uc — best effort
		if _, err := os.Stat("bin/ffmpeg"); err != nil {
			fc := exec.Command("sh", "-c",
				"mkdir -p bin && curl -fsSL https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz -o /tmp/ff.tar.xz "+
					"&& tar -xJf /tmp/ff.tar.xz -C /tmp --wildcards '*/ffmpeg' "+
					"&& cp /tmp/ffmpeg-*-amd64-static/ffmpeg bin/ffmpeg && chmod +x bin/ffmpeg && rm -f /tmp/ff.tar.xz")
			fc.Stdout, fc.Stderr = os.Stdout, os.Stderr
			if err := fc.Run(); err != nil {
				log.Printf("ffmpeg bootstrap gagal (thumbnail fallback nonaktif): %v", err)
			} else {
				log.Println("ffmpeg static terpasang")
			}
		}
		return exec.Command("python3", "-c", "import telethon, requests, cryptg").Run() == nil
	}
	if !bootstrap() {
		log.Println("uploader nonaktif — /get akan kirim link saja")
	}
	go func() {
		backoff := 2 * time.Second
		for {
			cmd := exec.Command("python3", "-u", "uploader.py")
			cmd.Stdout, cmd.Stderr = os.Stdout, os.Stderr
			cmd.Dir, _ = os.Getwd()
			cmd.Env = os.Environ()
			if err := cmd.Run(); err != nil {
				log.Printf("uploader mati: %v — restart %s", err, backoff)
			}
			time.Sleep(backoff)
			if backoff < 30*time.Second {
				backoff *= 2
			}
		}
	}()
}

// ---------------- main ----------------

func main() {
	log.SetFlags(log.LstdFlags | log.Lmsgprefix)
	log.SetPrefix("[nganu] ")
	if botToken == "" {
		log.Fatal("BOT_TOKEN belum diisi")
	}
	loadProxies()
	botUsername = getBotUsername()
	if botUsername == "" {
		log.Println("getMe gagal — deep link download tidak akan berfungsi")
	}
	port := envStr("PORT", "8080")
	startUploader()
	go func() { // health endpoint untuk platform hosting
		http.HandleFunc("/health", func(w http.ResponseWriter, r *http.Request) { w.Write([]byte("ok")) })
		log.Printf("health :%s/health", port)
		if err := http.ListenAndServe(":"+port, nil); err != nil {
			log.Printf("health server mati: %v", err)
		}
	}()

	log.Printf("bot mulai (owner=%d channel=%d maxUpload=%dGB)",
		ownerID, channelID, maxUploadBytes>>30)
	usersLoad()
	var offset int64
	for {
		ups, err := getUpdates(offset)
		if err != nil {
			log.Printf("getUpdates: %v (retry 3s)", err)
			time.Sleep(3 * time.Second)
			continue
		}
		for _, u := range ups {
			offset = int64(u.UpdateID) + 1
			dispatch := func() {
				defer func() {
					if r := recover(); r != nil {
						log.Printf("panic handler: %v", r)
					}
				}()
				switch {
				case u.Message != nil:
					handle(u.Message)
				case u.CallbackQuery != nil:
					handleCallback(u.CallbackQuery)
				}
			}
			go dispatch()
		}
	}
}
