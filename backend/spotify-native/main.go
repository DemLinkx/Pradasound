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
	"strings"
	"time"

	librespot "github.com/devgianlu/go-librespot"
	"github.com/devgianlu/go-librespot/ap"
	"github.com/devgianlu/go-librespot/apresolve"
	"github.com/devgianlu/go-librespot/login5"
	pbdata "github.com/devgianlu/go-librespot/proto/spotify/clienttoken/data/v0"
	pbhttp "github.com/devgianlu/go-librespot/proto/spotify/clienttoken/http/v0"
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
	ID     string `json:"id"`
	Title  string `json:"title"`
	Artist string `json:"artist"`
}

type output struct {
	Tracks []track `json:"tracks"`
	Cursor string  `json:"cursor"`
	Error  string  `json:"error,omitempty"`
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
	page, err := sp.CollectionPage(ctx, accesspoint.Username(), "collection", in.Cursor, 50)
	if err != nil {
		return fail("collection")
	}
	uris := []string{}
	for _, item := range page.GetItems() {
		if !item.GetIsRemoved() && strings.HasPrefix(item.GetUri(), "spotify:track:") {
			uris = append(uris, item.GetUri())
		}
	}
	result := output{Tracks: []track{}, Cursor: page.GetNextPageToken()}
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
			found[entity.GetEntityUri()] = track{ID: strings.TrimPrefix(entity.GetEntityUri(), "spotify:track:"), Title: info.GetName(), Artist: strings.Join(artists, ", ")}
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
