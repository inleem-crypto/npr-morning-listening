'use strict';
function lessonFreshness(data, status, episodes, now = Date.now()) {
  if (!data?.episode) return '';
  const published = Date.parse(data.episode.date);
  const newer = episodes.some(ep => Date.parse(ep.date) > published);
  const failed = status?.status === 'failed' &&
    Date.parse(status.attempted_at) > Date.parse(data.generated_at);
  const old = !Number.isFinite(published) || now - published > 18 * 60 * 60 * 1000;
  if (failed) return '최신 학습자료 갱신에 실패해 이전 자료를 보존했습니다. 현재 문제는 최신 회차가 아닐 수 있습니다.';
  if (newer || old) return '이전 방송의 학습자료입니다. 최신 회차의 딕테이션 문제는 아직 준비되지 않았습니다.';
  return '표시된 방송의 학습자료입니다. NPR은 수시로 새 회차를 공개하므로 방송 시각을 확인해 주세요.';
}
function lessonNeedsRefresh(data, status) {
  return status?.status === 'success' &&
    Date.parse(data.generated_at) < Date.parse(status.lesson_generated_at);
}
if (typeof module !== 'undefined') module.exports = {lessonFreshness, lessonNeedsRefresh};
