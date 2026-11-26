"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { apiFetch } from "@/app/lib/api";

/**
 * 노드망 편집 상태 — 맵마다 운영자가 그린 노드·간선(2026-10-19 노드망 경로).
 *
 * 화면은 노드 위치와 운영자가 직접 정한 간선(수동·문·제외)만 들고 있고, 자동 간선·장소 연결·점검 결과는
 * 서버가 맵 격자로 벽을 검사해 계산한다(POST validate — 저장하지 않음). 저장은 PUT 한 번.
 */

export type NavEdgeKind = "auto" | "manual" | "door" | "excluded";

export interface NavNode {
  key: string;          // 저장된 노드는 id 문자열, 새 노드는 "new-N"
  x: number;
  y: number;
}

export interface NavEdge {
  a: string;
  b: string;
  kind: NavEdgeKind;
  direction: "both" | "forward";
  doorId: number | null;
}

export interface NavIssue {
  level: "block" | "warn";
  kind: string;
  text: string;
  placeId?: number;
  nodeKey?: string | null;
  edge?: [string | number, string | number];   // kind=manual_wall: 벽을 지나는 수동 간선 양끝
}

export interface NavCheck {
  ready: boolean;
  reason: string;
  issues: NavIssue[];
  unlinkedPlaceIds: number[];
  placeAttach: Record<string, string | null>;   // 장소 id → 붙은 노드 key
}

/** 구간 → 노드망 이관 기록 — 원래 한쪽이던 구간은 양방향으로 옮겼다(운영자 확인 목록) */
export interface NavMigration {
  routes: number;
  oneWay: { a: string; b: string; from: string; to: string }[];
}

/** 간선 방향 — 양방향 / a→b / b→a */
export type EdgeDir = "both" | "ab" | "ba";

export interface RoutePreview {
  ok: boolean;
  msg?: string;
  nodePath?: number[];
  sent?: number[];
  entry?: boolean;
  lengthM?: number;
  ratio?: number | null;
}

const VALIDATE_DELAY_MS = 300;

export function useNavNodes(mapId: number | "") {
  const [nodes, setNodes] = useState<NavNode[]>([]);
  const [userEdges, setUserEdges] = useState<NavEdge[]>([]);   // 수동·문·제외만
  const [edges, setEdges] = useState<NavEdge[]>([]);           // 서버가 계산한 전체(자동 포함)
  const [check, setCheck] = useState<NavCheck | null>(null);
  // 서버의 노드망 경로가 켜져 있는가 — 꺼져 있으면 구간이 아직 이동에 쓰인다(맵 편집에 구간 도구를 남긴다)
  const [enabled, setEnabled] = useState(false);
  const [chargerNodes, setChargerNodes] = useState<string[]>([]);
  const [migration, setMigration] = useState<NavMigration | null>(null);
  const [dirty, setDirty] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const seq = useRef(0);
  const newSeq = useRef(0);

  const applyServer = useCallback((data: { nodes: { id?: number; key?: string; x: number; y: number }[];
    edges: { a: number | string; b: number | string; kind: NavEdgeKind; direction: "both" | "forward"; doorId: number | null }[];
    check: NavCheck; chargerNodes?: (number | string)[]; migration?: NavMigration; enabled?: boolean }) => {
    setNodes(data.nodes.map((n) => ({ key: String(n.key ?? n.id), x: n.x, y: n.y })));
    const all = data.edges.map((e) => ({ ...e, a: String(e.a), b: String(e.b) }));
    setEdges(all);
    setUserEdges(all.filter((e) => e.kind !== "auto"));
    setCheck(data.check);
    setEnabled(!!data.enabled);
    setChargerNodes((data.chargerNodes ?? []).map(String));
    if (data.migration) {
      setMigration({ ...data.migration, oneWay: data.migration.oneWay.map((m) => ({ ...m, a: String(m.a), b: String(m.b) })) });
    }
  }, []);

  const load = useCallback(async () => {
    if (mapId === "") return;
    setError(null);
    const res = await apiFetch(`/map/nav-nodes/${mapId}`);
    if (!res.ok) { setError("노드망을 불러오지 못했습니다."); return; }
    applyServer(await res.json());
    setDirty(0);
  }, [mapId, applyServer]);

  useEffect(() => { load(); }, [load]);

  // 편집할 때마다 서버에 검사만 맡긴다(저장 안 함) — 자동 간선·장소 연결·점검을 바로 다시 그린다
  useEffect(() => {
    if (mapId === "" || dirty === 0) return;
    const my = ++seq.current;
    const t = setTimeout(async () => {
      const res = await apiFetch(`/map/nav-nodes/${mapId}/validate`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          nodes: nodes.map((n) => ({ key: n.key, x: n.x, y: n.y })),
          edges: userEdges.map((e) => ({ a: e.a, b: e.b, kind: e.kind, direction: e.direction, door_id: e.doorId })),
        }),
      });
      if (my !== seq.current) return;
      const data = await res.json();
      if (!res.ok) { setError(data.detail ?? "검사에 실패했습니다."); return; }
      setError(null);
      setEdges(data.edges.map((e: NavEdge) => ({ ...e, a: String(e.a), b: String(e.b) })));
      setCheck(data.check);
      setChargerNodes((data.chargerNodes ?? []).map(String));
    }, VALIDATE_DELAY_MS);
    return () => clearTimeout(t);
  }, [mapId, dirty, nodes, userEdges]);

  const touch = () => setDirty((d) => d + 1);

  const addNode = (x: number, y: number) => {
    newSeq.current += 1;
    const key = `new-${newSeq.current}`;
    setNodes((ns) => [...ns, { key, x, y }]);
    touch();
    return key;
  };

  const moveNode = (key: string, x: number, y: number) => {
    setNodes((ns) => ns.map((n) => (n.key === key ? { ...n, x, y } : n)));
    touch();
  };

  /** 노드와 그 선을 지운다. block = 지운 뒤 이웃끼리 새로 생길 자동 연결 — 막은 연결로 넣어 다시 이어지지 않게 한다. */
  const removeNode = (key: string, block: [string, string][] = []) => {
    setNodes((ns) => ns.filter((n) => n.key !== key));
    setUserEdges((es) => [
      ...es.filter((e) => e.a !== key && e.b !== key),
      ...block.map(([a, b]) => ({ a, b, kind: "excluded" as const, direction: "both" as const, doorId: null })),
    ]);
    touch();
  };

  /** 두 노드를 잇는다. 자동 간선이 있으면 제외로 토글, 수동이 있으면 지운다. */
  const toggleLink = (a: string, b: string) => {
    const same = (e: NavEdge) => (e.a === a && e.b === b) || (e.a === b && e.b === a);
    const auto = edges.some((e) => e.kind === "auto" && same(e));
    setUserEdges((es) => {
      const mine = es.find(same);
      if (mine) return es.filter((e) => !same(e));
      return [...es, { a, b, kind: auto ? "excluded" : "manual", direction: "both", doorId: null }];
    });
    touch();
  };

  const addDoorEdge = (a: string, b: string, doorId: number, direction: "both" | "forward") => {
    const same = (e: NavEdge) => (e.a === a && e.b === b) || (e.a === b && e.b === a);
    setUserEdges((es) => [...es.filter((e) => !same(e)), { a, b, kind: "door", direction, doorId }]);
    touch();
  };

  /**
   * 간선 방향을 바꾼다. 자동 간선은 같은 자리에 수동 간선을 만들어 대신한다(서버가 자동 간선을 뺀다).
   * 한쪽 방향은 a→b 순서로 저장한다.
   */
  const setEdgeDirection = (a: string, b: string, dir: EdgeDir) => {
    const same = (e: NavEdge) => (e.a === a && e.b === b) || (e.a === b && e.b === a);
    const [from, to] = dir === "ba" ? [b, a] : [a, b];
    const direction = dir === "both" ? "both" : "forward";
    setUserEdges((es) => {
      const mine = es.find((e) => same(e) && e.kind !== "excluded");
      const rest = es.filter((e) => !same(e));
      return [...rest, { a: from, b: to, kind: mine?.kind ?? "manual", direction, doorId: mine?.doorId ?? null }];
    });
    touch();
  };

  const save = async (): Promise<string | null> => {
    if (mapId === "") return null;
    setSaving(true);
    try {
      const res = await apiFetch(`/map/nav-nodes/${mapId}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          nodes: nodes.map((n) => ({ key: n.key, x: n.x, y: n.y })),
          edges: userEdges.map((e) => ({ a: e.a, b: e.b, kind: e.kind, direction: e.direction, door_id: e.doorId })),
        }),
      });
      const data = await res.json();
      if (!res.ok) return data.detail ?? "저장하지 못했습니다.";
      applyServer(data);
      setDirty(0);
      return null;
    } finally {
      setSaving(false);
    }
  };

  const preview = async (fromX: number, fromY: number, toPlaceId: number): Promise<RoutePreview> => {
    if (mapId === "") return { ok: false, msg: "맵을 먼저 선택하세요." };
    const res = await apiFetch(`/map/nav-nodes/${mapId}/preview`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ from_x: fromX, from_y: fromY, to_place_id: toPlaceId }),
    });
    return res.json();
  };

  /**
   * key 노드를 지우면 점검 결과가 어떻게 되는가 — 저장하지 않고 서버 검사만.
   * 지운 자리 양옆이 새로 자동 연결되면(3 m 안·벽 없음) 그 쌍을 막은 연결로 넣고 다시 검사한다 —
   * 지우면 노드와 그 선만 사라져야 한다(사용자 10-22). block 은 removeNode 에 그대로 넘긴다.
   */
  const checkRemove = async (key: string): Promise<{ check: NavCheck; block: [string, string][] } | null> => {
    if (mapId === "") return null;
    const pair = (e: { a: string; b: string }) => [e.a, e.b].sort().join("|");
    const had = new Set(edges.filter((e) => e.kind === "auto").map(pair));
    const nodesLeft = nodes.filter((n) => n.key !== key).map((n) => ({ key: n.key, x: n.x, y: n.y }));
    const edgesLeft = userEdges.filter((e) => e.a !== key && e.b !== key)
      .map((e) => ({ a: e.a, b: e.b, kind: e.kind, direction: e.direction, door_id: e.doorId }));
    const run = async (extra: typeof edgesLeft) => {
      const res = await apiFetch(`/map/nav-nodes/${mapId}/validate`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ nodes: nodesLeft, edges: [...edgesLeft, ...extra] }),
      });
      return res.ok ? (await res.json()) as { check: NavCheck; edges: NavEdge[] } : null;
    };
    const first = await run([]);
    if (!first) return null;
    const block = first.edges
      .filter((e) => e.kind === "auto" && !had.has(pair({ a: String(e.a), b: String(e.b) })))
      .map((e) => [String(e.a), String(e.b)] as [string, string]);
    if (block.length === 0) return { check: first.check, block };
    const second = await run(block.map(([a, b]) => ({ a, b, kind: "excluded" as const, direction: "both" as const, door_id: null })));
    return second ? { check: second.check, block } : null;
  };

  /** 이렇게 바꿔도 서버가 받아 주는가 — 거절 이유(벽을 지나는 연결 등), 받으면 null. 저장하지 않는다. 확인 실패는 막지 않는다. */
  const rejectReason = async (ns: NavNode[], es: NavEdge[]): Promise<string | null> => {
    if (mapId === "") return null;
    try {
      const res = await apiFetch(`/map/nav-nodes/${mapId}/validate`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          nodes: ns.map((n) => ({ key: n.key, x: n.x, y: n.y })),
          edges: es.map((e) => ({ a: e.a, b: e.b, kind: e.kind, direction: e.direction, door_id: e.doorId })),
        }),
      });
      if (res.ok) return null;
      const d = await res.json().catch(() => ({}));
      return d.detail ?? null;
    } catch {
      return null;
    }
  };

  /** a–b 를 새로 이어도 되는가. 이미 이어져 있으면(끊는 것) 묻지 않는다. */
  const checkLink = async (a: string, b: string): Promise<string | null> => {
    const same = (e: NavEdge) => (e.a === a && e.b === b) || (e.a === b && e.b === a);
    if (userEdges.some(same) || edges.some((e) => e.kind === "auto" && same(e))) return null;
    return rejectReason(nodes, [...userEdges, { a, b, kind: "manual", direction: "both", doorId: null }]);
  };

  /** key 노드를 (x, y) 로 옮겨도 되는가 — 이어진 연결이 벽을 지나게 되면 거절 이유 */
  const checkMove = (key: string, x: number, y: number) =>
    rejectReason(nodes.map((n) => (n.key === key ? { ...n, x, y } : n)), userEdges);

  /** (x, y) 에 장소를 만들면 노드에 붙는가 — 편집 중 노드로 서버가 벽을 검사한다 */
  const checkAttach = async (x: number, y: number): Promise<{ nodeKey: string | null; distM?: number; nearestKey?: string | null; nearestM?: number | null; reason?: string }> => {
    if (mapId === "") return { nodeKey: null };
    const res = await apiFetch(`/map/nav-nodes/${mapId}/attach-check`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ x, y, nodes: nodes.map((n) => ({ key: n.key, x: n.x, y: n.y })) }),
    });
    return res.json();
  };

  /** (x, y) 에 노드를 둘 수 있는가 — 둘 수 있으면 null, 아니면 이유. 확인 실패는 막지 않는다(저장 때 서버가 다시 본다). */
  const checkSpot = async (x: number, y: number): Promise<string | null> => {
    if (mapId === "") return null;
    try {
      const res = await apiFetch(`/map/nav-nodes/${mapId}/spot-check`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ x, y }),
      });
      if (!res.ok) return null;
      const d = await res.json();
      return d.ok ? null : (d.reason ?? "노드를 둘 수 없는 곳입니다.");
    } catch {
      return null;
    }
  };

  return {
    checkSpot, checkLink, checkMove,
    nodes, edges, check, enabled, error, saving, dirty: dirty > 0, checkAttach, checkRemove, chargerNodes, migration,
    addNode, moveNode, removeNode, toggleLink, addDoorEdge, setEdgeDirection, save, reload: load, preview,
  };
}
