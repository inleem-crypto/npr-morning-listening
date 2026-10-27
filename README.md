NPR Morning Listening Lab v4

웹 주소: https://inleem-crypto.github.io/npr-morning-listening/

이 웹앱은 NPR News Now의 최신 음원을 공식 팟캐스트 피드에서 찾습니다. 본문부터 듣기,
속도 조절, 구간 반복, 듣기 메모를 지원합니다. 음원은 NPR의 원본 링크로 재생하며
저장소에 MP3를 보관하지 않습니다.

딕테이션 문제는 GitHub Actions의 "Prepare daily dictation lesson" 작업이 한국 시각
매일 05:15와 05:35에 최신 회차를 자동 전사해 lesson.json으로 만듭니다. 전사 결과와
정답은 사람이 검증한 대본이 아니므로 틀릴 수 있습니다. GitHub Actions 일정은
지연될 수 있으며 음원이나 전사 모델을 다운로드하지 못하면 기존 파일을 유지합니다.

웹앱은 lesson.json을 자동으로 읽고 해당 문제의 방송을 선택합니다. 문제 파일이
없거나 오래되었으면 준비 중이라는 메시지를 표시합니다. 수동 JSON 입력은 평소에
필요하지 않습니다.

배포 시 index.html, manifest.webmanifest, icon.svg, sw.js, requirements.txt,
scripts/generate_lesson.py, .github/workflows/daily-lesson.yml을 저장소 main 브랜치에
같은 상대 경로로 올리세요. GitHub Pages는 main / (root)로 둡니다. 첫 실행은
저장소 Actions 탭에서 Prepare daily dictation lesson을 선택하고 Run workflow를
누를 수 있습니다. 작업이 실패하면 해당 실행 기록에서 원인을 확인하세요.
