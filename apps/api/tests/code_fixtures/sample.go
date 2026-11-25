package store

import (
	"context"
	db "database/sql"
)

import "fmt"

const Version = "1.0"

const (
	MaxConns = 10
	minConns = 1
)

type (
	ID string
	Pair struct{ A, B int }
)

// Store keeps things. func Fake() {} in a comment
type Store struct {
	db *db.DB
}

type Reader interface {
	Read(ctx context.Context) error
}

func New(d *db.DB) *Store {
	s := `raw { string`
	_ = s
	return &Store{db: d}
}

func (s *Store) Get(ctx context.Context, id ID) (string, error) {
	if id == "" {
		return "", fmt.Errorf("empty")
	}
	return string(id), nil
}

func Map[T any, U any](in []T, f func(T) U) []U { return nil }
