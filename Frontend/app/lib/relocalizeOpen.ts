import { useSyncExternalStore } from "react";

/**
 * 위치 재조정 창이 어디서든 열려 있는지.
 *
 * 재조정 창은 대시보드·알림·안내 시나리오 화면 등 여러 곳에서 연다. 그동안 로봇은 아직 미확정이라
 * 전역 '위치 확인 필요/층 이동 중' 안내창이 재조정 창 위에 겹쳐 뜨지 않도록 이 값을 본다.
 */
let openCount = 0;
const listeners = new Set<() => void>();

function emit() {
  listeners.forEach((l) => l());
}

/** 재조정 창을 열 때 부른다. 돌려준 함수로 닫힘을 알린다. */
export function markRelocalizeOpen(): () => void {
  openCount += 1;
  emit();
  let closed = false;
  return () => {
    if (closed) return;
    closed = true;
    openCount -= 1;
    emit();
  };
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

export function useRelocalizeOpen(): boolean {
  return useSyncExternalStore(
    subscribe,
    () => openCount > 0,
    () => false
  );
}
