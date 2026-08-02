package main

import "net/http"

type Server struct{}

func (s *Server) Handle() {}

func main() {
	http.ListenAndServe(":8080", nil)
}
