import asyncio
import json
import os
import sys

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

from services.extractor import find_first_url, resolve_track_url

RAW_INPUT = """
1. https://www.youtube.com/watch?v=4NRXx6U8ABQ (YouTube, /watch)
2. https://www.youtube.com/watch?v=JGwWNGJdvx8 (YouTube, /watch)
3. https://www.youtube.com/watch?v=DyDfgMOUjCI (YouTube, /watch)
4. https://www.youtube.com/watch?v=kXYiU_JCYtU (YouTube, /watch)
5. https://www.youtube.com/watch?v=qU9mHegkTc4 (YouTube, /watch)
6. https://www.youtube.com/watch?v=sVx1mJDeUjY (YouTube, /watch)
7. https://www.youtube.com/watch?v=K5KAc5CoCuk (YouTube, /watch)
8. https://www.youtube.com/watch?v=Ijk4j-r7qPA (YouTube, /watch)
9. https://www.youtube.com/watch?v=5anLPw0Efmo (YouTube, /watch)
10. https://www.youtube.com/watch?v=1uYWYWPc9HU (YouTube, /watch)
11. https://www.youtube.com/watch?v=HyHNuVaZJ-k (YouTube, /watch)
12. https://www.youtube.com/watch?v=0J2QdDbelmY (YouTube, /watch)
13. https://www.youtube.com/watch?v=bpOSxM0rNPM (YouTube, /watch, official)
14. https://www.youtube.com/watch?v=GCdwKhTtNNw (YouTube, /watch, official)
15. https://www.youtube.com/watch?v=izGwDsrQ1eQ (YouTube, /watch)

16. https://youtu.be/4NRXx6U8ABQ (YouTube, youtu.be)
17. https://youtu.be/JGwWNGJdvx8 (YouTube, youtu.be)
18. https://youtu.be/DyDfgMOUjCI (YouTube, youtu.be)
19. https://youtu.be/kXYiU_JCYtU (YouTube, youtu.be)
20. https://youtu.be/qU9mHegkTc4 (YouTube, youtu.be)
21. https://youtu.be/sVx1mJDeUjY (YouTube, youtu.be)
22. https://youtu.be/K5KAc5CoCuk (YouTube, youtu.be)
23. https://youtu.be/5anLPw0Efmo (YouTube, youtu.be)
24. https://youtu.be/HyHNuVaZJ-k (YouTube, youtu.be)
25. https://youtu.be/GCdwKhTtNNw (YouTube, youtu.be)

26. https://music.youtube.com/watch?v=4NRXx6U8ABQ (YouTube Music)
27. https://music.youtube.com/watch?v=JGwWNGJdvx8 (YouTube Music)
28. https://music.youtube.com/watch?v=DyDfgMOUjCI (YouTube Music)
29. https://music.youtube.com/watch?v=kXYiU_JCYtU (YouTube Music)
30. https://music.youtube.com/watch?v=qU9mHegkTc4 (YouTube Music)
31. https://music.youtube.com/watch?v=sVx1mJDeUjY (YouTube Music)
32. https://music.youtube.com/watch?v=K5KAc5CoCuk (YouTube Music)
33. https://music.youtube.com/watch?v=5anLPw0Efmo (YouTube Music)
34. https://music.youtube.com/watch?v=bpOSxM0rNPM (YouTube Music)
35. https://music.youtube.com/watch?v=GCdwKhTtNNw (YouTube Music)

36. https://open.spotify.com/track/2Zzt4wOHJqtWI05ztyn4Xm (Spotify, standard)
37. https://open.spotify.com/track/2dUXeTmHcBXi6nuFonTi3j (Spotify, standard)
38. https://open.spotify.com/track/0OVcmnCipBPfSm5pMK9ICO (Spotify, standard)
39. https://open.spotify.com/track/7bF2JoUdwD1AoETsFmpphz (Spotify, standard)
40. https://open.spotify.com/track/3tp8SzO03whXySKeBd4E2A (Spotify, standard)
41. https://open.spotify.com/track/5IQf5yT9NfWgKBIOUMt86s (Spotify, standard)
42. https://open.spotify.com/track/3emlHcAwIh8Ka2iIOD13Rc (Spotify, standard)
43. https://open.spotify.com/track/017dcJuCg52UEqKoZCGPuK (Spotify, soundtrack)
44. https://open.spotify.com/track/0Y9cEDh5aHVFYFgCgBRVHj (Spotify, standard)
45. https://open.spotify.com/track/3SYkxKBdwKFCTxWDh9l5f9f (Spotify, artist-like negative test)
46. https://open.spotify.com/intl-de/track/2Zzt4wOHJqtWI05ztyn4Xm (Spotify, /intl-de/)
47. https://open.spotify.com/intl-de/track/2dUXeTmHcBXi6nuFonTi3j (Spotify, /intl-de/)
48. https://open.spotify.com/intl-de/track/0OVcmnCipBPfSm5pMK9ICO (Spotify, /intl-de/)
49. https://open.spotify.com/track/3tp8SzO03whXySKeBd4E2A?si=test123 (Spotify, query)
50. https://open.spotify.com/track/2Zzt4wOHJqtWI05ztyn4Xm?si=test456&utm_source=copy-link (Spotify, query)
51. https://open.spotify.com/track/2dUXeTmHcBXi6nuFonTi3j?utm_source=copy-link (Spotify, query)

52. https://music.apple.com/us/song/1558199052 (Apple Music, song)
53. https://music.apple.com/us/song/1440666126 (Apple Music, song)
54. https://music.apple.com/us/song/1702245668 (Apple Music, song)
55. https://music.apple.com/us/song/1176303886 (Apple Music, song)
56. https://music.apple.com/us/song/488235001 (Apple Music, song)
57. https://music.apple.com/us/song/1570504177 (Apple Music, song)
58. https://music.apple.com/us/song/1751829988 (Apple Music, song)
59. https://music.apple.com/ru/song/1439426971 (Apple Music, /ru/)
60. https://music.apple.com/ru/song/1570208715 (Apple Music, /ru/)
61. https://music.apple.com/us/song/1680715891 (Apple Music, Cyrillic)
62. https://music.apple.com/de/song/1439426971 (Apple Music, /de/)
63. https://music.apple.com/de/song/1627673695 (Apple Music, /de/)
64. https://music.apple.com/ru/album/%D0%BA%D0%BE%D1%80%D0%BE%D0%BB%D1%8C-%D0%B8-%D1%88%D1%83%D1%82-%D0%BE%D1%84%D0%B8%D1%86%D0%B8%D0%B0%D0%BB%D1%8C%D0%BD%D1%8B%D0%B9-%D1%81%D0%B0%D1%83%D0%BD%D0%B4%D1%82%D1%80%D0%B5%D0%BA-%D1%87%D0%B0%D1%81%D1%82%D1%8C-1/1680715880 (Apple Music, album)
65. https://music.apple.com/cd/album/indila-derni%C3%A8re-danse-single/1590927657 (Apple Music, international album)

66. https://soundcloud.com/theweeknd/blinding-lights (SoundCloud, artist/track)
67. https://soundcloud.com/theweeknd/the-weeknd-blinding-lights (SoundCloud, artist/track)
68. https://soundcloud.com/billieeilish/bad-guy (SoundCloud, artist/track)
69. https://soundcloud.com/spyralvst/afterdark (SoundCloud, remix)
70. https://soundcloud.com/jordantilstone/mr-kitty-after-dark-jordan (SoundCloud, remix)
71. https://soundcloud.com/othersidewave/mrkitty-after-dark-otherside-remix (SoundCloud, remix)
72. https://soundcloud.com/fxri-edit/after-dark-mr-kitty-edit-audio (SoundCloud, edit)
73. https://soundcloud.com/platinum-xv/mr-kitty-after-dark-8d-slowed-reverb (SoundCloud, slowed)
74. https://soundcloud.com/user-44647389/mr-kitty-after-dark-slowed (SoundCloud, slowed)
75. https://soundcloud.com/theweeknd/sets/blinding-lights-255187406 (SoundCloud, playlist/track set)

76. https://www.youtube.com/shorts/4NRXx6U8ABQ (YouTube, Shorts-format test)
77. https://www.youtube.com/shorts/JGwWNGJdvx8 (YouTube, Shorts-format test)
78. https://m.youtube.com/watch?v=4NRXx6U8ABQ (YouTube, mobile)
79. https://m.youtube.com/watch?v=sVx1mJDeUjY (YouTube, mobile)
80. https://m.youtube.com/watch?v=bpOSxM0rNPM (YouTube, mobile)

81. https://open.spotify.com/track/0cQVqPuHQP4KEwc7ZUQmj6 (Spotify, embed-style ID)
82. https://open.spotify.com/track/7MYHci3U9oUSwipkN31dsZ (Spotify, alternate release)
83. https://open.spotify.com/track/5y4PGyHu7Gc8zcC11RGOJC (Spotify, live/remastered)
84. https://open.spotify.com/track/67oMmhYXiULjcviyjX98z0 (Spotify, live)
85. https://open.spotify.com/track/36i9HyLz4ziVtELURFg6jf (Spotify, album version)
86. https://open.spotify.com/track/5fpaRyKGjC6gUDU1n6HHP7 (Spotify, soundtrack)
87. https://open.spotify.com/track/7bF2JoUdwD1AoETsFmpphz (Spotify, mixed)
88. https://open.spotify.com/track/4AiobhZLu2mloCQjCmjDdA (Spotify, release variant)
89. https://open.spotify.com/track/2LKOHdMsL0K9KwcPRlJK2v?si=8c6c8aef40df4084 (Spotify, query)
90. https://open.spotify.com/track/3VT8sYk5anWjQ4KLLfdT46 (Spotify, Cyrillic/Latin mixed title)

91. https://music.apple.com/us/song/1176303886?l=en-US (Apple Music, query)
92. https://music.apple.com/de/song/1439426971?l=en-DE (Apple Music, query)
93. https://music.apple.com/us/song/1570504177?l=en-US (Apple Music, query)
94. https://music.apple.com/us/song/488235001?l=en-US (Apple Music, query)
95. https://music.apple.com/ru/song/1680715891 (Apple Music, Cyrillic track)
96. https://soundcloud.com/theweeknd/blinding-lights (SoundCloud, duplicate-platform regression)
97. https://soundcloud.com/theweeknd/the-weeknd-blinding-lights (SoundCloud, instrumental)
98. https://soundcloud.com/billieeilish/bad-guy (SoundCloud, official artist URL)
99. https://www.youtube.com/watch?v=bpOSxM0rNPM?utm_source=test (YouTube, query)
100. https://music.youtube.com/watch?v=sVx1mJDeUjY&si=test123 (YouTube Music, query)
"""

async def main():
    lines = [l.strip() for l in RAW_INPUT.strip().split("\n") if l.strip()]
    results = []
    
    print(f"Loaded {len(lines)} lines for testing.")
    
    for i, line in enumerate(lines, 1):
        url = find_first_url(line)
        if not url:
            print(f"[{i:03d}] FAIL_URL: {line}")
            results.append({"num": i, "status": "FAIL_URL", "input": line})
            continue
            
        try:
            track = await resolve_track_url(url)
            print(f"[{i:03d}] OK [{track.platform}] {track.display_name} -> {track.target[:50]}")
            results.append({
                "num": i,
                "input": line,
                "url": url,
                "platform": track.platform,
                "display_name": track.display_name,
                "target": track.target,
                "is_search": track.is_search,
                "has_thumb": bool(track.thumbnail_url),
                "status": "SUCCESS"
            })
        except Exception as e:
            print(f"[{i:03d}] ERROR: {url} -> {e}")
            results.append({"num": i, "input": line, "url": url, "status": f"ERROR: {e}"})
            
    with open("test_results.json", "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
        
    success = sum(1 for r in results if r.get("status") == "SUCCESS")
    print(f"\nFinal Result: {success}/{len(lines)} succeeded ({success/len(lines)*100:.1f}%)")

if __name__ == "__main__":
    asyncio.run(main())
