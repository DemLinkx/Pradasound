package main

import (
	"context"
	"testing"

	collectionpb "github.com/devgianlu/go-librespot/proto/spotify/collection/v2"
)

func TestSkipPagesWithoutLiveTracks(t *testing.T) {
	pages := map[string]*collectionpb.PageResponse{
		"":       {Items: []*collectionpb.CollectionItem{{Uri: "spotify:album:album"}, {Uri: "spotify:track:deleted", IsRemoved: true}}, NextPageToken: "second"},
		"second": {NextPageToken: "third"},
		"third":  {Items: []*collectionpb.CollectionItem{{Uri: "spotify:track:song"}}, NextPageToken: "fourth"},
	}
	fetch := func(_ context.Context, username, set, cursor string, limit int) (*collectionpb.PageResponse, error) {
		if username != "owner" || set != "collection" || limit != 50 {
			t.Fatal("wrong collection")
		}
		return pages[cursor], nil
	}
	uris, next, stats, err := scanCollection(context.Background(), fetch, "owner", "")
	if err != nil || len(uris) != 1 || uris[0] != "spotify:track:song" || next != "fourth" {
		t.Fatalf("lost tracks after sparse pages: %v %v", uris, err)
	}
	if stats.Pages != 3 || stats.Items != 3 || stats.Removed != 1 || stats.Other != 1 {
		t.Fatalf("bad counters: %+v", stats)
	}
}

func TestRepeatedCursorStopsInsteadOfReportingEmpty(t *testing.T) {
	fetch := func(_ context.Context, _, _, _ string, _ int) (*collectionpb.PageResponse, error) {
		return &collectionpb.PageResponse{NextPageToken: "repeat"}, nil
	}
	_, _, _, err := scanCollection(context.Background(), fetch, "owner", "repeat")
	if err == nil {
		t.Fatal("repeated cursor accepted")
	}
}

func TestTerminalEmptyCollection(t *testing.T) {
	fetch := func(_ context.Context, _, _, _ string, _ int) (*collectionpb.PageResponse, error) {
		return &collectionpb.PageResponse{SyncToken: "confirmed"}, nil
	}
	uris, next, stats, err := scanCollection(context.Background(), fetch, "owner", "")
	if err != nil || len(uris) != 0 || next != "" || stats.Pages != 1 {
		t.Fatal("invalid empty result")
	}
}
