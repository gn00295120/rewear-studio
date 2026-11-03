#!/bin/sh
# Synthetic test clip: slow horizontal pan over a still image. Usage: make_pan_video.sh image out.mp4 [seconds] [fps]
set -e
S=${3:-8}; F=${4:-10}
ffmpeg -v error -y -loop 1 -t "$S" -i "$1" -vf "scale=1920:-2,crop=1280:720:x='(iw-ow)*t/$S':y='(ih-oh)/2',fps=$F,format=yuv420p" -c:v libx264 -movflags +faststart "$2"
ffprobe -v error -select_streams v:0 -show_entries stream=width,height,r_frame_rate,nb_frames -of csv=p=0 "$2"
