const {test} = require('node:test');
const assert = require('node:assert/strict');
const {lessonFreshness, lessonNeedsRefresh} = require('../lesson-status.js');
const now = Date.parse('2026-11-02T21:00:00Z');
const data = {episode:{date:'2026-11-02T20:00:00Z'},generated_at:'2026-11-02T20:10:00Z'};
test('recent lesson identifies its broadcast without claiming latest',()=>{
  assert.match(lessonFreshness(data,null,[],now),/표시된 방송/);
});
test('newer broadcast and old broadcast both warn',()=>{
  assert.match(lessonFreshness(data,null,[{date:'2026-11-02T20:30:00Z'}],now),/이전 방송/);
  assert.match(lessonFreshness(data,null,[],now+24*3600000),/이전 방송/);
});
test('failed attempt warns, even if lesson has not changed',()=>{
  assert.match(lessonFreshness(data,{status:'failed',attempted_at:'2026-11-02T20:50:00Z'},[],now),/갱신에 실패/);
});
test('old failure does not override recovered lesson',()=>{
  assert.doesNotMatch(lessonFreshness(data,{status:'failed',attempted_at:'2026-11-01T20:50:00Z'},[],now),/갱신에 실패/);
});
test('generation timestamp cannot disguise old broadcast',()=>{
  assert.match(lessonFreshness({...data,episode:{date:'2026-10-31T20:00:00Z'}},null,[],now),/이전 방송/);
});
test('older raw file uses alternate fetch, current or newer files stay',()=>{
  const status = {status:'success',lesson_generated_at:'2026-11-02T20:20:00Z'};
  assert.equal(lessonNeedsRefresh(data,status),true);
  assert.equal(lessonNeedsRefresh({...data,generated_at:status.lesson_generated_at},status),false);
  assert.equal(lessonNeedsRefresh({...data,generated_at:'2026-11-02T20:30:00Z'},status),false);
  assert.equal(lessonNeedsRefresh(data,null),false);
});
