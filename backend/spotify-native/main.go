// Metadata-only Spotify session reader. Protocol: go-librespot (GPL-3.0).
package main

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"os"
	"strconv"
	"strings"
	"time"

	librespot "github.com/devgianlu/go-librespot"
	"github.com/devgianlu/go-librespot/ap"
	"github.com/devgianlu/go-librespot/apresolve"
	"github.com/devgianlu/go-librespot/login5"
	pbdata "github.com/devgianlu/go-librespot/proto/spotify/clienttoken/data/v0"
	pbhttp "github.com/devgianlu/go-librespot/proto/spotify/clienttoken/http/v0"
	collectionpb "github.com/devgianlu/go-librespot/proto/spotify/collection/v2"
	extpb "github.com/devgianlu/go-librespot/proto/spotify/extendedmetadata"
	credpb "github.com/devgianlu/go-librespot/proto/spotify/login5/v3/credentials"
	metapb "github.com/devgianlu/go-librespot/proto/spotify/metadata"
	"github.com/devgianlu/go-librespot/spclient"
	"google.golang.org/protobuf/proto"
)

type input struct {
	Token  string `json:"token"`
	Cursor string `json:"cursor"`
}

type track struct {
	ID       string `json:"id"`
	Title    string `json:"title"`
	Artist   string `json:"artist"`
	Duration int32  `json:"duration_ms"`
}

type output struct {
	Tracks  []track   `json:"tracks"`
	Cursor  string    `json:"cursor"`
	Error   string    `json:"error,omitempty"`
	Account string    `json:"account,omitempty"`
	Stats   scanStats `json:"stats"`
}

type scanStats struct {
	Pages   int  `json:"pages"`
	Items   int  `json:"items"`
	Removed int  `json:"removed"`
	Other   int  `json:"other"`
	Unknown int  `json:"unknown"`
	Context bool `json:"context"`
}

type collectionRead func(context.Context, string, string, string, int) (*collectionpb.PageResponse, error)

func scanCollection(ctx context.Context, fetch collectionRead, username, cursor string) ([]string, string, scanStats, error) {
	stats := scanStats{}
	seen := map[string]bool{}
	for stats.Pages < 50 {
		if seen[cursor] {
			return nil, "", stats, errors.New("pagination")
		}
		seen[cursor] = true
		page, err := fetch(ctx, username, "collection", cursor, 50)
		if err != nil {
			return nil, "", stats, err
		}
		stats.Pages++
		stats.Items += len(page.GetItems())
		stats.Unknown += len(page.ProtoReflect().GetUnknown())
		uris := []string{}
		for _, item := range page.GetItems() {
			if item.GetIsRemoved() {
				stats.Removed++
				continue
			}
			if strings.HasPrefix(item.GetUri(), "spotify:track:") {
				uris = append(uris, item.GetUri())
			} else {
				stats.Other++
			}
		}
		cursor = page.GetNextPageToken()
		if len(uris) > 0 || cursor == "" {
			return uris, cursor, stats, nil
		}
	}
	return nil, "", stats, errors.New("pagination_limit")
}

func contextTracks(ctx context.Context, sp *spclient.Spclient, username string, offset int) ([]string, string, error) {
	contextData, err := sp.ContextResolve(ctx, "spotify:user:"+username+":collection")
	if err != nil {
		return nil, "", err
	}
	resolver, err := spclient.NewContextResolver(ctx, &librespot.NullLogger{}, sp, contextData)
	if err != nil {
		return nil, "", err
	}
	uris := []string{}
	position := 0
	for page := 0; page < 10; page++ {
		tracks, err := resolver.Page(ctx, page)
		if errors.Is(err, io.EOF) {
			return uris, "", nil
		}
		if err != nil {
			return nil, "", err
		}
		for _, item := range tracks {
			uri := item.GetUri()
			if uri == "" && len(item.GetGid()) == 16 {
				uri = "spotify:track:" + librespot.GidToBase62(item.GetGid())
			}
			if !strings.HasPrefix(uri, "spotify:track:") {
				continue
			}
			if position >= offset {
				if len(uris) == 50 {
					return uris, "ctx:" + strconv.Itoa(offset+50), nil
				}
				uris = append(uris, uri)
			}
			position++
		}
	}
	return nil, "", errors.New("context_limit")
}

func clientToken(ctx context.Context, client *http.Client, device string) (string, error) {
	body, err := proto.Marshal(&pbhttp.ClientTokenRequest{
		RequestType: pbhttp.ClientTokenRequestType_REQUEST_CLIENT_DATA_REQUEST,
		Request: &pbhttp.ClientTokenRequest_ClientData{ClientData: &pbhttp.ClientDataRequest{
			ClientId: librespot.ClientIdHex, ClientVersion: librespot.SpotifyLikeClientVersion(),
			Data: &pbhttp.ClientDataRequest_ConnectivitySdkData{ConnectivitySdkData: &pbdata.ConnectivitySdkData{
				DeviceId: device, PlatformSpecificData: librespot.GetPlatformSpecificData(),
			}},
		}},
	})
	if err != nil {
		return "", err
	}
	req, err := http.NewRequestWithContext(ctx, "POST", "https://clienttoken.spotify.com/v1/clienttoken", bytes.NewReader(body))
	if err != nil {
		return "", err
	}
	req.Header.Set("Accept", "application/x-protobuf")
	req.Header.Set("User-Agent", librespot.UserAgent())
	resp, err := client.Do(req)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		return "", errors.New("client_token")
	}
	data, err := io.ReadAll(io.LimitReader(resp.Body, 65536))
	if err != nil {
		return "", err
	}
	var result pbhttp.ClientTokenResponse
	if err = proto.Unmarshal(data, &result); err != nil {
		return "", err
	}
	if result.GetGrantedToken().GetToken() == "" {
		return "", errors.New("client_token")
	}
	return result.GetGrantedToken().GetToken(), nil
}

func read(ctx context.Context, in input) output {
	fail := func(stage string) output { return output{Error: stage} }
	client := &http.Client{Timeout: 15 * time.Second, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
	deviceBytes := make([]byte, 20)
	if _, err := rand.Read(deviceBytes); err != nil {
		return fail("device")
	}
	device := hex.EncodeToString(deviceBytes)
	log := &librespot.NullLogger{}
	resolver := apresolve.NewApResolver(log, client, true)
	addr, err := resolver.GetAccesspoint(ctx)
	if err != nil {
		return fail("resolve")
	}
	accesspoint := ap.NewAccesspoint(log, addr, device)
	defer accesspoint.Close()
	if err = accesspoint.ConnectSpotifyToken(ctx, "", in.Token); err != nil {
		return fail("session")
	}
	ct, err := clientToken(ctx, client, device)
	if err != nil {
		return fail("client_token")
	}
	login := login5.NewLogin5(log, client, device, ct)
	if err = login.Login(ctx, &credpb.StoredCredential{Username: accesspoint.Username(), Data: accesspoint.StoredCredentials()}); err != nil {
		return fail("login5")
	}
	spAddr, err := resolver.GetSpclient(ctx)
	if err != nil {
		return fail("resolve")
	}
	sp, err := spclient.NewSpclient(ctx, log, client, spAddr, login.AccessToken(), device, ct)
	if err != nil {
		return fail("session")
	}
	username := login.Username()
	var uris []string
	next := ""
	stats := scanStats{}
	if strings.HasPrefix(in.Cursor, "ctx:") {
		offset, parseErr := strconv.Atoi(strings.TrimPrefix(in.Cursor, "ctx:"))
		if parseErr != nil || offset < 0 || offset >= 500 {
			return fail("input")
		}
		stats.Context = true
		uris, next, err = contextTracks(ctx, sp, username, offset)
	} else {
		uris, next, stats, err = scanCollection(ctx, sp.CollectionPage, username, in.Cursor)
		if err == nil && len(uris) == 0 && in.Cursor == "" {
			stats.Context = true
			uris, next, err = contextTracks(ctx, sp, username, 0)
		}
	}
	result := output{Tracks: []track{}, Cursor: next, Account: username, Stats: stats}
	if err != nil {
		result.Error = "collection"
		return result
	}
	if len(uris) == 0 {
		return result
	}
	request := &extpb.BatchedEntityRequest{}
	for _, uri := range uris {
		request.EntityRequest = append(request.EntityRequest, &extpb.EntityRequest{
			EntityUri: uri, Query: []*extpb.ExtensionQuery{{ExtensionKind: extpb.ExtensionKind_TRACK_V4}},
		})
	}
	metadata, err := sp.ExtendedMetadata(ctx, request)
	if err != nil {
		return fail("metadata")
	}
	found := map[string]track{}
	for _, group := range metadata.GetExtendedMetadata() {
		if group.GetExtensionKind() != extpb.ExtensionKind_TRACK_V4 {
			continue
		}
		for _, entity := range group.GetExtensionData() {
			if entity.GetHeader().GetStatusCode() != 200 || entity.GetExtensionData() == nil {
				continue
			}
			var info metapb.Track
			if proto.Unmarshal(entity.GetExtensionData().GetValue(), &info) != nil || info.GetName() == "" {
				continue
			}
			artists := []string{}
			for _, artist := range info.GetArtist() {
				if artist.GetName() != "" {
					artists = append(artists, artist.GetName())
				}
			}
			found[entity.GetEntityUri()] = track{ID: strings.TrimPrefix(entity.GetEntityUri(), "spotify:track:"), Title: info.GetName(), Artist: strings.Join(artists, ", "), Duration: info.GetDuration()}
		}
	}
	for _, uri := range uris {
		if item, ok := found[uri]; ok {
			result.Tracks = append(result.Tracks, item)
		}
	}
	if len(result.Tracks) == 0 {
		return fail("metadata")
	}
	return result
}

func main() {
	var in input
	if json.NewDecoder(io.LimitReader(os.Stdin, 32768)).Decode(&in) != nil || len(in.Token) < 20 || len(in.Token) > 8192 || len(in.Cursor) > 8192 {
		_ = json.NewEncoder(os.Stdout).Encode(output{Error: "input"})
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), 40*time.Second)
	defer cancel()
	_ = json.NewEncoder(os.Stdout).Encode(read(ctx, in))
}
