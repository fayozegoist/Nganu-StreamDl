package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"mime/multipart"
	"net/http"
	"time"
)

var (
	botToken   = envStr("BOT_TOKEN", "")
	tgAPI      = "https://api.telegram.org/bot" + botToken
	tgHTTP     = &http.Client{Timeout: 65 * time.Second}
	tgPollHTTP = &http.Client{Timeout: 40 * time.Second}
)

type tgResponse struct {
	OK          bool            `json:"ok"`
	Result      json.RawMessage `json:"result"`
	Description string          `json:"description"`
	ErrorCode   int             `json:"error_code"`
}

type Update struct {
	UpdateID      int            `json:"update_id"`
	Message       *Message       `json:"message"`
	CallbackQuery *CallbackQuery `json:"callback_query"`
}

type CallbackQuery struct {
	ID   string `json:"id"`
	Data string `json:"data"`
	From *struct {
		ID int64 `json:"id"`
	} `json:"from"`
	Message *Message `json:"message"`
}

// tbtn: tombol inline keyboard (hanya untuk navigasi halaman).
type tbtn struct {
	Text string `json:"text"`
	Data string `json:"callback_data"`
}

type Message struct {
	MessageID int    `json:"message_id"`
	Text      string `json:"text"`
	Chat      struct {
		ID int64 `json:"id"`
	} `json:"chat"`
	From *struct {
		ID        int64  `json:"id"`
		Username  string `json:"username"`
		FirstName string `json:"first_name"`
	} `json:"from"`
}

type sentMessage struct {
	MessageID int `json:"message_id"`
}

func tgCall(method string, payload map[string]any) (json.RawMessage, error) {
	body, err := json.Marshal(payload)
	if err != nil {
		return nil, err
	}
	resp, err := tgHTTP.Post(tgAPI+"/"+method, "application/json", bytes.NewReader(body))
	if err != nil {
		return nil, fmt.Errorf("[TG] %s gagal: %w", method, err)
	}
	defer resp.Body.Close()
	data, _ := io.ReadAll(io.LimitReader(resp.Body, 4<<20))
	var r tgResponse
	if err := json.Unmarshal(data, &r); err != nil {
		return nil, fmt.Errorf("[TG] %s: respons bukan JSON (%d)", method, resp.StatusCode)
	}
	if !r.OK {
		return nil, fmt.Errorf("[TG] %s: %s", method, r.Description)
	}
	return r.Result, nil
}

func getUpdates(offset int64) ([]Update, error) {
	body, _ := json.Marshal(map[string]any{
		"offset": offset, "timeout": 30, "allowed_updates": []string{"message", "callback_query"},
	})
	resp, err := tgPollHTTP.Post(tgAPI+"/getUpdates", "application/json", bytes.NewReader(body))
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	var r struct {
		OK     bool     `json:"ok"`
		Result []Update `json:"result"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&r); err != nil {
		return nil, err
	}
	if !r.OK {
		return nil, fmt.Errorf("getUpdates tidak ok")
	}
	return r.Result, nil
}

func sendHTML(chatID int64, text string, rm ...map[string]any) (*sentMessage, error) {
	payload := map[string]any{
		"chat_id": chatID, "text": text, "parse_mode": "html",
		"disable_web_page_preview": true,
	}
	if len(rm) > 0 && rm[0] != nil {
		payload["reply_markup"] = rm[0]
	}
	raw, err := tgCall("sendMessage", payload)
	if err != nil {
		return nil, err
	}
	var m sentMessage
	json.Unmarshal(raw, &m)
	return &m, nil
}

func editHTML(chatID int64, msgID int, text string, rm ...map[string]any) error {
	payload := map[string]any{
		"chat_id": chatID, "message_id": msgID, "text": text,
		"parse_mode": "html", "disable_web_page_preview": true,
	}
	if len(rm) > 0 && rm[0] != nil {
		payload["reply_markup"] = rm[0]
	}
	_, err := tgCall("editMessageText", payload)
	return err
}

func answerCallback(id, text string, alert bool) {
	tgCall("answerCallbackQuery", map[string]any{
		"callback_query_id": id, "text": text, "show_alert": alert,
	})
}

func sendDoc(chatID int64, data []byte, filename, caption string) error {
	var buf bytes.Buffer
	w := multipart.NewWriter(&buf)
	w.WriteField("chat_id", fmt.Sprint(chatID))
	if caption != "" {
		w.WriteField("caption", caption)
	}
	part, _ := w.CreateFormFile("document", filename)
	part.Write(data)
	w.Close()
	req, _ := http.NewRequest("POST", tgAPI+"/sendDocument", &buf)
	req.Header.Set("Content-Type", w.FormDataContentType())
	resp, err := tgHTTP.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(io.LimitReader(resp.Body, 4<<20))
	var r tgResponse
	if err := json.Unmarshal(body, &r); err != nil || !r.OK {
		return fmt.Errorf("[TG] sendDocument: %s", r.Description)
	}
	return nil
}

func deleteMessage(chatID int64, msgID int) {
	tgCall("deleteMessage", map[string]any{"chat_id": chatID, "message_id": msgID})
}
