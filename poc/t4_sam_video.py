"""SAM 3.1 video: upload via Files API (fallback: data URL), track one concept, analyse identity stability, render overlay MP4."""
import asyncio, sys, json, pathlib, time, subprocess, shutil, collections
from PIL import Image
from common import client, OUT, save_json
from sam_common import segment, data_url, overlay, rec_to_dict

def probe(path):
    out = subprocess.run(["ffprobe","-v","error","-select_streams","v:0","-show_entries","stream=width,height,r_frame_rate,nb_frames","-of","json",str(path)],capture_output=True,text=True).stdout
    s = json.loads(out)["streams"][0]; num, den = s["r_frame_rate"].split("/")
    return int(s["width"]), int(s["height"]), float(num)/float(den), int(s.get("nb_frames") or 0)

async def main(path, concept):
    path = pathlib.Path(path); W, H, fps, n = probe(path)
    print(f"video {path.name}: {W}x{H} @ {fps:.2f}fps, {n} frames, {path.stat().st_size/1e6:.1f} MB")
    part = None
    try:
        f = client().files.create(file=open(path, "rb"), purpose="user_data")
        part = {"type": "input_video", "file_id": f.id}; print("uploaded file_id", f.id)
    except Exception as e:
        print("Files API failed, falling back to data URL:", str(e)[:200]); part = {"type": "input_video", "video_url": data_url(path)}
    t = time.time(); records, result, raw = await segment(part, concept, video=True); dt = time.time() - t
    (OUT / f"t4_{path.stem}_{concept.replace(' ','_')}_raw.txt").write_text(raw or "")
    boxes = [r for r in records if r.kind == "box"]; masks = [r for r in records if r.kind == "mask"]
    frames = sorted({r.frame for r in boxes}); ids = sorted({r.object_id for r in boxes}, key=str)
    per_frame = collections.Counter(r.frame for r in boxes)
    # identity stability: for each object id, frames present, longest gap, box-centre jump stats
    traj = collections.defaultdict(list)
    for r in sorted(boxes, key=lambda r: (str(r.object_id), r.frame)):
        traj[r.object_id].append((r.frame, (r.left+r.right)/2, (r.top+r.bottom)/2))
    stab = {}
    for oid, pts in traj.items():
        fr = [p[0] for p in pts]; gaps = [b-a for a, b in zip(fr, fr[1:])]
        jumps = [((x2-x1)**2+(y2-y1)**2)**0.5 for (_,x1,y1),(_,x2,y2) in zip(pts, pts[1:])]
        stab[str(oid)] = {"frames": len(fr), "first": fr[0], "last": fr[-1], "max_gap": max(gaps) if gaps else 0,
                          "mean_jump_px": sum(jumps)/len(jumps) if jumps else 0, "max_jump_px": max(jumps) if jumps else 0}
    print(f"[{concept}] {dt:.1f}s frames_with_hits={len(frames)}/{n} ids={len(ids)} boxes={len(boxes)} masks={len(masks)} max_per_frame={max(per_frame.values()) if per_frame else 0}")
    for oid, s in stab.items(): print(f"  id {oid}: frames {s['frames']} [{s['first']}..{s['last']}] max_gap {s['max_gap']} mean_jump {s['mean_jump_px']:.1f}px max_jump {s['max_jump_px']:.1f}px")
    save_json(f"t4_{path.stem}_{concept.replace(' ','_')}_summary.json", {"seconds": dt, "video": {"w": W, "h": H, "fps": fps, "frames": n},
              "ids": [str(i) for i in ids], "stability": stab, "records": [rec_to_dict(r) for r in records][:5000]})
    # overlay video: extract frames → draw → encode
    if boxes:
        fd = OUT / f"t4_{path.stem}_frames"; shutil.rmtree(fd, ignore_errors=True); fd.mkdir()
        subprocess.run(["ffmpeg","-v","error","-i",str(path),"-start_number","0",str(fd/"%05d.png")],check=True)
        od = OUT / f"t4_{path.stem}_overlay"; shutil.rmtree(od, ignore_errors=True); od.mkdir()
        by_frame = collections.defaultdict(list)
        for r in records: by_frame[getattr(r, "frame", 0)].append(r)
        for i, fp in enumerate(sorted(fd.glob("*.png"))):
            im = Image.open(fp); overlay(im, by_frame.get(i, []), frame=i).save(od / fp.name)
        outmp4 = OUT / f"t4_{path.stem}_{concept.replace(' ','_')}_overlay.mp4"
        subprocess.run(["ffmpeg","-v","error","-y","-framerate",str(fps),"-i",str(od/"%05d.png"),"-c:v","libx264","-pix_fmt","yuv420p","-movflags","+faststart",str(outmp4)],check=True)
        print("overlay video:", outmp4)

if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "garment"))
