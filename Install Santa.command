#!/bin/bash
# Double-click to install Santa on this Mac (no admin rights needed).
# It runs santa-app/Install Santa.command; see READ ME FIRST.txt.
exec bash "$(cd "$(dirname "$0")" && pwd)/santa-app/Install Santa.command"
