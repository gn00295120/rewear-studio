#!/bin/sh
# Downsample a phone clip for SAM (10 fps, 720p, H.264, faststart). Usage: prep_video.sh in.mov out.mp4 [fps]
set -e
ffmpeg -v error -y -i "$1" -vf "fps=${3:-10},scale=-2:720" -an -c:v libx264 -preset fast -crf 23 -movflags +faststart "$2"
ffprobe -v error -select_streams v:0 -show_entries stream=width,height,r_frame_rate,nb_frames -of csv=p=0 "$2"
