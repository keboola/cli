---------------------------- MODULE SyncEngine ----------------------------
(***************************************************************************)
(* Finite model of the kbagent GitOps sync engine (`kbagent sync          *)
(* pull|diff|push`), issue keboola/cli#792.                                *)
(*                                                                         *)
(* Every operator below mirrors a specific piece of the Python code; the   *)
(* file:line references are to this repository at the time of writing:     *)
(*   services/sync_service.py   pull() 482-1128, diff() 1232-1568,         *)
(*                              push() 1574-1966,                          *)
(*                              _resolve_source_branch_path 2387-2420      *)
(*   sync/diff_engine.py        compute_changeset 308-443                  *)
(*   sync/branch_scope.py       scope_manifest 133-211,                    *)
(*                              classify_untracked 244-279,                *)
(*                              find_untracked_configs 307-373             *)
(*   services/_sync_writeback.py stamp_created_config / stamp_updated_config*)
(*                              / writeback_create_config_in_manifest      *)
(*   services/_sync_baseline.py detect_force_pull_conflicts / _is_conflict *)
(*                                                                         *)
(* Abstractions (see README.md): rows, renames, config_hash_version        *)
(* migration, extra code files, dry-run, name-collision suffixes and       *)
(* unstampable read-backs are not modelled. Content is a small integer;    *)
(* the RAW file hash (sha256 of _config.yml bytes) and the NORMALIZED       *)
(* config hash (config_hash) are separate abstract values.                 *)
(***************************************************************************)
EXTENDS Naturals, FiniteSets, Sequences, TLC

CONSTANTS
    MaxSteps,       \* depth bound (state constraint)
    NV,             \* content versions 0..NV-1
    InitNeverFetched, \* TRUE: start with a legacy never-fetched entry for i1
    MaxScaffolds,   \* how many `config new` scaffolds the user may create
    EnableAbort,    \* model ENCRYPTION_FAILED aborts inside push
    EnableRemote,   \* enable the concurrent remote actor
    EnableIgnore,   \* enable "component becomes ignored"
    EnableDev       \* allow operations against the dev branch

Branches == {"prod", "dev"}
Trees    == {"main", "devt"}
TreeOf(b) == IF b = "prod" THEN "main" ELSE "devt"
DefaultTree == "main"                    \* manifest.branches[0].path

InitIds == {"i1", "i2"}
NewIds  == {"n1", "n2"}
Ids     == InitIds \cup NewIds
Paths   == {"p1", "p2", "q1", "q2", "ps"}
DefPath(id) == CASE id = "i1" -> "p1" [] id = "i2" -> "p2"
                 [] id = "n1" -> "q1" [] id = "n2" -> "q2"
V == 0..(NV - 1)
IgnComp == "mcp"                         \* e.g. keboola.mcp-server-tool
Sids == {"s1", "s2"}                     \* lineage ids for scaffolded files

OpBranches == IF EnableDev THEN Branches ELSE {"prod"}

(* ---- value encodings (records always carry an `ex` flag so TLC never   *)
(* compares values of different type)                                    *)

\* Local _config.yml. comp/cid/nm are the `_keboola` block + name (part of
\* the RAW bytes, stripped by normalization); v = logical content;
\* cosm = a byte-level-only difference (key order, whitespace, is_disabled:
\* false vs absent) that changes the RAW hash but not config_hash;
\* drift = the local representation hashes differently from the API's view
\* of the SAME logical content (#686: multi-statement script shape).
\* Ghost fields: sid = lineage (which remote config / scaffold this file
\* descends from), ed = holds user work not yet on any remote, sv = the
\* remote content version this file was last synced with (pull or push).
NoFile == [ex |-> FALSE, comp |-> "tx", cid |-> "none", nm |-> "p1", v |-> 0,
           cosm |-> FALSE, drift |-> FALSE, sid |-> "none", ed |-> FALSE, sv |-> 99]

\* Remote configuration on one branch. org = lineage of the local file /
\* remote actor that created it.
Absent == [ex |-> FALSE, v |-> 0, nm |-> "p1", comp |-> "tx", org |-> "none"]

\* Hashes. RAW hash = everything in the file except ghost fields.
Raw(f)      == <<f.comp, f.cid, f.nm, f.v, f.cosm, f.drift>>
NoHash      == <<"", "", "", 99, FALSE, FALSE>>
NormLocal(f) == <<f.v, f.drift>>             \* config_hash(local file)
ApiH(r)      == <<r.v, FALSE>>               \* config_hash(api_config_to_local(remote))
NoBase       == <<99, FALSE>>

\* ManifestConfiguration: branchId, path, componentId, metadata.pull_hash,
\* metadata.pull_config_hash.
NoEntry == [ex |-> FALSE, br |-> "prod", path |-> "p1", comp |-> "tx",
            ph |-> NoHash, base |-> NoBase]

NoOp == [kind |-> "init", b |-> "prod", wrote |-> {}, force |-> FALSE,
         i2 |-> TRUE, i2b |-> TRUE, i3 |-> TRUE, i7 |-> TRUE,
         lost |-> TRUE, resur |-> TRUE, loss |-> TRUE, abortWrites |-> {}]

VARIABLES fl, mn, rm, ig, used, userDel, nextSid, lastOp, steps
vars == <<fl, mn, rm, ig, used, userDel, nextSid, lastOp, steps>>

St == [fl |-> fl, mn |-> mn, rm |-> rm, ig |-> ig, used |-> used]

(***************************************************************************)
(* diff() -- pure function of the state                                    *)
(***************************************************************************)
Ign(st) == IF st.ig THEN {IgnComp} ELSE {}

\* _resolve_source_branch_path (sync_service.py:2387-2420): the target
\* branch's tree if it holds any config, else the default tree (promote).
Src(st, b) == IF \E p \in Paths : st.fl[TreeOf(b)][p].ex THEN TreeOf(b) ELSE DefaultTree

\* remote_configs, ignored components filtered (sync_service.py:1281-1282)
RKeys(st, b) == {id \in Ids : st.rm[b][id].ex /\ st.rm[b][id].comp \notin Ign(st)}

\* scope_manifest (branch_scope.py:164-199)
Live(st) == {id \in Ids : st.mn[id].ex /\ st.mn[id].comp \notin Ign(st)}
EntryTree(st, id) == TreeOf(st.mn[id].br)
NeverFetched(st) == {id \in Live(st) : st.mn[id].ph = NoHash
                                      /\ ~st.fl[EntryTree(st, id)][st.mn[id].path].ex}
Claimed(st) == Live(st) \ NeverFetched(st)
InTree(st, S) == {id \in Claimed(st) : EntryTree(st, id) = S}

\* find_untracked_configs (branch_scope.py:331-371): tracked_paths is built
\* from ALL manifest entries (ignored and never-fetched included).
TrackedPaths(st, S) == {st.mn[id].path : id \in {i \in Ids : st.mn[i].ex /\ EntryTree(st, i) = S}}
Untracked(st, S) == {p \in Paths : st.fl[S][p].ex /\ p \notin TrackedPaths(st, S)
                                    /\ st.fl[S][p].comp \notin Ign(st)}   \* :1394

\* classify_untracked (branch_scope.py:244-279)
Verdict(st, b, S, p) ==
    LET cid == st.fl[S][p].cid
        claims == IF cid \in Claimed(st) THEN {EntryTree(st, cid)} ELSE {}
    IN  IF cid = "none" THEN "create"
        ELSE IF S \in claims THEN "create"
        ELSE IF cid \in RKeys(st, b) THEN "adopt"
        ELSE IF claims # {} THEN "orphan"
        ELSE "create"

\* in-tree locals (sync_service.py:1320-1384) + base_hashes (:1441-1452)
Unchanged(st, S, id) == st.mn[id].ph # NoHash /\ Raw(st.fl[S][st.mn[id].path]) = st.mn[id].ph
TrackedLocals(st, S) ==
    { [id |-> id, path |-> st.mn[id].path, trk |-> TRUE,
       base |-> IF st.mn[id].base # NoBase THEN st.mn[id].base
                ELSE IF Unchanged(st, S, id) THEN NormLocal(st.fl[S][st.mn[id].path])
                ELSE NoBase,
       lh |-> IF Unchanged(st, S, id) /\ st.mn[id].base # NoBase THEN st.mn[id].base
              ELSE NormLocal(st.fl[S][st.mn[id].path])]
      : id \in {i \in InTree(st, S) : st.fl[S][st.mn[i].path].ex} }

UntrackedLocals(st, b, S) ==
    { [id |-> IF Verdict(st, b, S, p) = "adopt" THEN st.fl[S][p].cid ELSE "none",
       path |-> p, trk |-> FALSE, base |-> NoBase, lh |-> NormLocal(st.fl[S][p])]
      : p \in {q \in Untracked(st, S) : Verdict(st, b, S, q) # "orphan"} }

Locals(st, b) == LET S == Src(st, b) IN TrackedLocals(st, S) \cup UntrackedLocals(st, b, S)

\* compute_changeset (diff_engine.py:352-402)
Kind(st, b, le) ==
    IF le.id = "none" \/ le.id \notin RKeys(st, b) THEN "added"
    ELSE LET rh == ApiH(st.rm[b][le.id]) IN
         IF le.lh = rh THEN "none"
         ELSE IF le.base = NoBase THEN "modified"
         ELSE LET lc == le.lh # le.base
                  rc == rh # le.base
              IN IF lc /\ rc THEN "conflict"
                 ELSE IF rc THEN "remote_modified"
                 ELSE "modified"

\* diff_engine.py:417-441: tracked (in-tree) remote keys no local entry saw
Deleted(st, b) ==
    LET S == Src(st, b)
        seen == {le.id : le \in Locals(st, b)}
    IN  {id \in RKeys(st, b) \cap InTree(st, S) : id \notin seen}

Changes(st, b) ==
    LET S == Src(st, b) IN
    { [kind |-> Kind(st, b, le), id |-> le.id, path |-> le.path, trk |-> le.trk,
       comp |-> st.fl[S][le.path].comp, base |-> le.base]
      : le \in {l \in Locals(st, b) : Kind(st, b, l) # "none"} }
    \cup
    { [kind |-> "deleted", id |-> id, path |-> st.mn[id].path, trk |-> TRUE,
       comp |-> st.mn[id].comp, base |-> st.mn[id].base] : id \in Deleted(st, b) }

Pushable(st, b) == {c \in Changes(st, b) : c.kind \in {"added", "modified", "deleted"}}
KindOf(st, b, id) ==
    LET cs == {c \in Changes(st, b) : c.id = id} IN
    IF cs = {} THEN "none" ELSE (CHOOSE c \in cs : TRUE).kind

(***************************************************************************)
(* pull() -- sync_service.py:482-1128                                      *)
(***************************************************************************)
PPathFor(st, b, id) == IF st.mn[id].ex THEN st.mn[id].path ELSE st.rm[b][id].nm
PFile(st, b, id) == st.fl[TreeOf(b)][PPathFor(st, b, id)]

\* :767-774
LocMod(st, b, mode, id) ==
    /\ st.mn[id].ex /\ mode # "theirs" /\ st.mn[id].ph # NoHash
    /\ PFile(st, b, id).ex /\ Raw(PFile(st, b, id)) # st.mn[id].ph
\* :829-850
RemUnch(st, b, mode, id) ==
    /\ st.mn[id].ex /\ st.mn[id].br = b /\ st.mn[id].base # NoBase
    /\ st.mn[id].base = ApiH(st.rm[b][id]) /\ PFile(st, b, id).ex
    /\ (mode = "theirs" => Raw(PFile(st, b, id)) = st.mn[id].ph)
Written(st, b, mode, id) == ~LocMod(st, b, mode, id) /\ ~RemUnch(st, b, mode, id)
PulledFile(st, b, id) ==
    LET r == st.rm[b][id] IN
    [ex |-> TRUE, comp |-> r.comp, cid |-> id, nm |-> r.nm, v |-> r.v,
     cosm |-> FALSE, drift |-> FALSE, sid |-> r.org, ed |-> FALSE, sv |-> r.v]
\* :990-1051
PEntry(st, b, mode, id) ==
    IF LocMod(st, b, mode, id)
    THEN [ex |-> TRUE, br |-> b, path |-> PPathFor(st, b, id), comp |-> st.rm[b][id].comp,
          ph |-> st.mn[id].ph, base |-> st.mn[id].base]
    ELSE [ex |-> TRUE, br |-> b, path |-> PPathFor(st, b, id), comp |-> st.rm[b][id].comp,
          ph |-> IF Written(st, b, mode, id) THEN Raw(PulledFile(st, b, id))
                 ELSE Raw(PFile(st, b, id)),
          base |-> ApiH(st.rm[b][id])]

\* detect_force_pull_conflicts / _is_conflict (_sync_baseline.py:423-529)
ForceConflict(st, b) ==
    \E id \in RKeys(st, b) :
        /\ st.mn[id].ex /\ st.mn[id].ph # NoHash /\ st.mn[id].base # NoBase
        /\ PFile(st, b, id).ex
        /\ Raw(PFile(st, b, id)) # st.mn[id].ph
        /\ ApiH(st.rm[b][id]) # st.mn[id].base

\* two fetched configs landing on one path: the code appends an id suffix
\* (:726-729); not modelled -- such pulls are disabled.
PullPathsDistinct(st, b) ==
    \A x, y \in RKeys(st, b) : x # y => PPathFor(st, b, x) # PPathFor(st, b, y)

\* stale-entry sweep (:1053-1094): against the WHOLE old manifest, rmtree
\* under the pulled branch's tree, AFTER the fetch loop wrote its files.
Stale(st, b) == {id \in Ids : st.mn[id].ex /\ id \notin RKeys(st, b)}
StalePaths(st, b) == {st.mn[id].path : id \in Stale(st, b)}
IgnoredStalePaths(st, b) == {st.mn[id].path : id \in {i \in Stale(st, b) : st.mn[i].comp \in Ign(st)}}

PullFiles(st, b, mode) ==
    LET T == TreeOf(b) IN
    [t \in Trees |-> IF t # T THEN st.fl[t] ELSE
        [p \in Paths |->
            IF p \in StalePaths(st, b) THEN NoFile
            ELSE IF \E id \in RKeys(st, b) : PPathFor(st, b, id) = p /\ Written(st, b, mode, id)
                 THEN PulledFile(st, b, CHOOSE id \in RKeys(st, b) :
                                          PPathFor(st, b, id) = p /\ Written(st, b, mode, id))
                 ELSE st.fl[T][p]]]

PullState(st, b, mode) ==
    [st EXCEPT !.fl = PullFiles(st, b, mode),
               !.mn = [id \in Ids |-> IF id \in RKeys(st, b) THEN PEntry(st, b, mode, id)
                                      ELSE NoEntry]]

Destroys(old, new) == old.ex /\ (~new.ex \/ new.v # old.v \/ new.drift # old.drift)

(***************************************************************************)
(* push() -- sync_service.py:1574-1966 (Phase A, configs only)              *)
(***************************************************************************)
PathIdx(p) == CASE p = "p1" -> 1 [] p = "p2" -> 2 [] p = "q1" -> 3 [] p = "q2" -> 4 [] p = "ps" -> 5
IdIdx(i) == CASE i = "none" -> 0 [] i = "i1" -> 1 [] i = "i2" -> 2 [] i = "n1" -> 3 [] i = "n2" -> 4
Key(c) == PathIdx(c.path) * 10 + IdIdx(c.id)
MinC(cs) == CHOOSE c \in cs : \A d \in cs : Key(c) <= Key(d)
FreeIds(st) == NewIds \ st.used
FreshId(st) == CHOOSE n \in FreeIds(st) : \A m \in FreeIds(st) : IdIdx(n) <= IdIdx(m)

ApplyOne(st, c, b, S) ==
    CASE c.kind = "added" ->
        \* push_create + writeback_after_push (new id written into the file)
        \* + stamp_created_config / writeback_create_config_in_manifest
        LET nid == FreshId(st)
            f == st.fl[S][c.path]
            nf == [f EXCEPT !.cid = nid, !.ed = FALSE, !.sv = f.v]
            r == [ex |-> TRUE, v |-> f.v, nm |-> f.nm, comp |-> f.comp, org |-> f.sid]
            match == {i \in Ids : st.mn[i].ex /\ st.mn[i].br = b /\ st.mn[i].comp = c.comp
                                  /\ st.mn[i].path = c.path}
            old == IF match # {} THEN CHOOSE i \in match : TRUE ELSE "none"
            e == IF old # "none" THEN st.mn[old]
                 ELSE [ex |-> TRUE, br |-> b, path |-> c.path, comp |-> c.comp,
                       ph |-> NoHash, base |-> NoBase]
            ne == [e EXCEPT !.ph = Raw(nf), !.base = ApiH(r)]
        IN [st EXCEPT !.rm[b][nid] = r, !.fl[S][c.path] = nf, !.used = @ \cup {nid},
                      !.mn = [i \in Ids |-> IF i = nid THEN ne
                                            ELSE IF i = old THEN NoEntry ELSE st.mn[i]]]
    [] c.kind = "modified" ->
        \* push_update + stamp_updated_config: entry looked up by id ONLY
        \* (no branch filter), created if missing (adopt-by-id).
        LET f == st.fl[S][c.path]
            r == [st.rm[b][c.id] EXCEPT !.v = f.v]
            ne == IF st.mn[c.id].ex THEN [st.mn[c.id] EXCEPT !.ph = Raw(f), !.base = ApiH(r)]
                  ELSE [ex |-> TRUE, br |-> b, path |-> c.path, comp |-> c.comp,
                        ph |-> Raw(f), base |-> ApiH(r)]
        IN [st EXCEPT !.rm[b][c.id] = r, !.fl[S][c.path].ed = FALSE,
                      !.fl[S][c.path].sv = f.v, !.mn[c.id] = ne]
    [] c.kind = "deleted" ->
        \* client.delete_config unconditionally (:1826-1838); force unused
        [st EXCEPT !.rm[b][c.id] = Absent, !.mn[c.id] = NoEntry]

RECURSIVE ApplyAll(_, _, _, _)
ApplyAll(st, cs, b, S) ==
    IF cs = {} THEN st ELSE LET c == MinC(cs) IN ApplyAll(ApplyOne(st, c, b, S), cs \ {c}, b, S)

WritesOf(cs, st0, st1, b) ==
    {<<b, i>> : i \in {j \in Ids : st1.rm[b][j] # st0.rm[b][j]}}

(***************************************************************************)
(* Initial state: production pulled once into main/                        *)
(***************************************************************************)
InitRem == [id \in Ids |->
    IF id = "i1" THEN [ex |-> TRUE, v |-> 0, nm |-> "p1", comp |-> "tx", org |-> "i1"]
    ELSE IF id = "i2" THEN [ex |-> TRUE, v |-> 0, nm |-> "p2", comp |-> IgnComp, org |-> "i2"]
    ELSE Absent]
PF(id) == [ex |-> TRUE, comp |-> InitRem[id].comp, cid |-> id, nm |-> InitRem[id].nm,
           v |-> 0, cosm |-> FALSE, drift |-> FALSE, sid |-> id, ed |-> FALSE, sv |-> 0]

Init ==
    /\ rm = [b \in Branches |-> InitRem]          \* dev branch = copy of prod
    /\ fl = [t \in Trees |-> [p \in Paths |->
              IF t = "main" /\ p = "p1" /\ ~InitNeverFetched THEN PF("i1")
              ELSE IF t = "main" /\ p = "p2" THEN PF("i2") ELSE NoFile]]
    /\ mn = [id \in Ids |->
              IF id \in InitIds
              THEN [ex |-> TRUE, br |-> "prod", path |-> DefPath(id), comp |-> InitRem[id].comp,
                    ph |-> IF id = "i1" /\ InitNeverFetched THEN NoHash ELSE Raw(PF(id)),
                    base |-> ApiH(InitRem[id])]
              ELSE NoEntry]
    /\ ig = FALSE
    /\ used = InitIds
    /\ userDel = [t \in Trees |-> {}]
    /\ nextSid = 1
    /\ lastOp = NoOp
    /\ steps = 0

Op(k, b) == [NoOp EXCEPT !.kind = k, !.b = b]
Tick == steps' = steps + 1

(***************************************************************************)
(* User actions                                                            *)
(***************************************************************************)
UEdit(t, p, dr) ==
    /\ fl[t][p].ex
    /\ fl' = [fl EXCEPT ![t][p].v = (@ + 1) % NV, ![t][p].drift = dr, ![t][p].ed = TRUE]
    /\ lastOp' = Op("user_edit", "prod") /\ Tick
    /\ UNCHANGED <<mn, rm, ig, used, userDel, nextSid>>

UCosmetic(t, p) ==
    /\ fl[t][p].ex /\ ~fl[t][p].cosm
    /\ fl' = [fl EXCEPT ![t][p].cosm = TRUE]
    /\ lastOp' = Op("user_cosmetic", "prod") /\ Tick
    /\ UNCHANGED <<mn, rm, ig, used, userDel, nextSid>>

UDeleteDir(t, p) ==
    /\ fl[t][p].ex
    /\ fl' = [fl EXCEPT ![t][p] = NoFile]
    /\ userDel' = [userDel EXCEPT ![t] = @ \cup {p}]
    /\ lastOp' = Op("user_rm", "prod") /\ Tick
    /\ UNCHANGED <<mn, rm, ig, used, nextSid>>

SidOf(k) == IF k = 1 THEN "s1" ELSE "s2"

\* `kbagent config new` scaffold (no --push): untracked file, no id
UScaffold(t) ==
    /\ nextSid <= MaxScaffolds /\ ~fl[t]["ps"].ex
    /\ fl' = [fl EXCEPT ![t]["ps"] = [ex |-> TRUE, comp |-> "tx", cid |-> "none", nm |-> "ps",
                                      v |-> 0, cosm |-> FALSE, drift |-> FALSE,
                                      sid |-> SidOf(nextSid), ed |-> TRUE, sv |-> 99]]
    /\ nextSid' = nextSid + 1
    /\ userDel' = [userDel EXCEPT ![t] = @ \ {"ps"}]
    /\ lastOp' = Op("user_scaffold", "prod") /\ Tick
    /\ UNCHANGED <<mn, rm, ig, used>>

\* `kbagent config new --push --output-dir`: creates the remote config and
\* writes the scaffold, carrying _keboola.config_id, into the branch's tree
\* (#644). No manifest entry is written.
UScaffoldPush(b) ==
    /\ nextSid <= MaxScaffolds /\ FreeIds(St) # {} /\ ~fl[TreeOf(b)]["ps"].ex
    /\ LET nid == FreshId(St) IN
       /\ rm' = [rm EXCEPT ![b][nid] = [ex |-> TRUE, v |-> 0, nm |-> "ps", comp |-> "tx",
                                        org |-> SidOf(nextSid)]]
       /\ fl' = [fl EXCEPT ![TreeOf(b)]["ps"] =
                   [ex |-> TRUE, comp |-> "tx", cid |-> nid, nm |-> "ps", v |-> 0,
                    cosm |-> FALSE, drift |-> FALSE, sid |-> SidOf(nextSid), ed |-> FALSE,
                    sv |-> 0]]
       /\ used' = used \cup {nid}
    /\ nextSid' = nextSid + 1
    /\ userDel' = [userDel EXCEPT ![TreeOf(b)] = @ \ {"ps"}]
    /\ lastOp' = Op("user_scaffold_push", b) /\ Tick
    /\ UNCHANGED <<mn, ig>>

\* component added to manifest.ignoredComponents (or hardcoded by an upgrade)
BecomeIgnored ==
    /\ ~ig /\ ig' = TRUE
    /\ lastOp' = Op("ignore", "prod") /\ Tick
    /\ UNCHANGED <<fl, mn, rm, used, userDel, nextSid>>

(***************************************************************************)
(* Remote actor (web UI / API / another kbagent)                           *)
(***************************************************************************)
REdit(b, id) ==
    /\ rm[b][id].ex
    /\ rm' = [rm EXCEPT ![b][id].v = (@ + 1) % NV]
    /\ lastOp' = Op("remote_edit", b) /\ Tick
    /\ UNCHANGED <<fl, mn, ig, used, userDel, nextSid>>

\* delete or trash: sync never queries the trash, both look like absence
RDelete(b, id) ==
    /\ rm[b][id].ex
    /\ rm' = [rm EXCEPT ![b][id] = Absent]
    /\ lastOp' = Op("remote_delete", b) /\ Tick
    /\ UNCHANGED <<fl, mn, ig, used, userDel, nextSid>>

\* create a config; its name may reuse the name of i1 (dir "p1")
RCreate(b, nm) ==
    /\ FreeIds(St) # {}
    /\ LET nid == FreshId(St) IN
       /\ rm' = [rm EXCEPT ![b][nid] = [ex |-> TRUE, v |-> 0, nm |-> nm, comp |-> "tx", org |-> nid]]
       /\ used' = used \cup {nid}
    /\ lastOp' = Op("remote_create", b) /\ Tick
    /\ UNCHANGED <<fl, mn, ig, userDel, nextSid>>

(***************************************************************************)
(* kbagent sync pull [--force | --theirs] [--branch b]                      *)
(***************************************************************************)
Pull(b, mode) ==
    /\ PullPathsDistinct(St, b)
    /\ ~(mode = "force" /\ ForceConflict(St, b))     \* SYNC_CONFLICT abort = no-op
    /\ LET ns == PullState(St, b, mode)
           T == TreeOf(b)
           lossPaths == {p \in Paths : fl[T][p].ex /\ fl[T][p].ed
                                       /\ Destroys(fl[T][p], ns.fl[T][p])
                                       /\ p \notin IgnoredStalePaths(St, b)}
       IN /\ fl' = ns.fl /\ mn' = ns.mn
          /\ userDel' = [userDel EXCEPT ![T] = {p \in @ : ~ns.fl[T][p].ex}]
          /\ lastOp' = [Op("pull_" \o mode, b) EXCEPT !.loss = (mode = "theirs" \/ lossPaths = {})]
    /\ Tick
    /\ UNCHANGED <<rm, ig, used, nextSid>>

(***************************************************************************)
(* kbagent sync push [--force] [--branch b]                                *)
(***************************************************************************)
\* `force` is not an argument: push() never reads it (sync_service.py:1574-1966),
\* so `sync push` and `sync push --force` are the same transition. The model
\* runs push WITHOUT --force and I2b checks the CLI's documented contract.
PushFlags(cs, b, S) ==
    LET dels == {c \in cs : c.kind = "deleted"} IN
    [NoOp EXCEPT
        !.force = FALSE,
        \* I2: every planned remote DELETE traces to a user `rm -rf`
        !.i2 = \A c \in dels : c.path \in userDel[S],
        \* I2b: CLI help "--force: Allow deletion of remote configs ..."
        !.i2b = (dels = {}),
        \* I3: nothing of an ignored component is planned
        !.i3 = \A c \in cs : c.comp \notin Ign(St),
        \* I7: no never-fetched entry is planned as a delete
        !.i7 = \A c \in dels : c.id \notin NeverFetched(St),
        \* I11: a "modified" decided WITHOUT a 3-way base (2-way fallback,
        \* diff_engine.py:389-391 -- adopt-by-id) only overwrites a remote value
        \* this file was synced with; otherwise a remote edit is silently lost.
        \* (With a base, remote_changed => conflict/remote_modified, never pushed.)
        !.lost = \A c \in cs : (c.kind = "modified" /\ c.base = NoBase)
                                   => rm[b][c.id].v = fl[S][c.path].sv,
        \* I11b: no tracked config deleted on the remote is silently re-created
        !.resur = \A c \in cs : ~(c.kind = "added" /\ c.trk)]

Push(b) ==
    LET cs == Pushable(St, b)
        S == Src(St, b)
        nadd == Cardinality({c \in cs : c.kind = "added"})
    IN
    /\ cs # {}
    /\ nadd <= Cardinality(FreeIds(St))                 \* id-pool bound
    /\ LET ns == ApplyAll(St, cs, b, S) IN
       /\ fl' = ns.fl /\ mn' = ns.mn /\ rm' = ns.rm /\ used' = ns.used
       /\ lastOp' = [PushFlags(cs, b, S) EXCEPT !.kind = "push", !.b = b,
                        !.wrote = WritesOf(cs, St, ns, b)]
    /\ Tick
    /\ UNCHANGED <<ig, userDel, nextSid>>

\* ENCRYPTION_FAILED on change k: changes ordered before k already reached
\* the API (and wrote the new id into their files); the exception propagates
\* past save_manifest (:1842-1852, :1945), so the manifest is NOT updated.
PushAbort(b) ==
    LET cs == Pushable(St, b)
        S == Src(St, b)
    IN
    /\ EnableAbort
    /\ Cardinality(cs) >= 2
    /\ Cardinality({c \in cs : c.kind = "added"}) <= Cardinality(FreeIds(St))
    /\ \E k \in cs :
         LET pre == {c \in cs : Key(c) < Key(k)}
             ns == ApplyAll(St, pre, b, S)
         IN /\ pre # {}
            /\ fl' = ns.fl /\ rm' = ns.rm /\ used' = ns.used
            /\ mn' = mn
            /\ lastOp' = [PushFlags(cs, b, S) EXCEPT !.kind = "push_abort", !.b = b,
                             !.abortWrites = WritesOf(pre, St, ns, b)]
    /\ Tick
    /\ UNCHANGED <<ig, userDel, nextSid>>

Next ==
    \/ \E t \in Trees, p \in Paths, dr \in BOOLEAN : UEdit(t, p, dr)
    \/ \E t \in Trees, p \in Paths : UCosmetic(t, p) \/ UDeleteDir(t, p)
    \/ \E t \in Trees : UScaffold(t)
    \/ \E b \in OpBranches : UScaffoldPush(b)
    \/ (EnableIgnore /\ BecomeIgnored)
    \/ EnableRemote /\ \E b \in OpBranches, id \in Ids : REdit(b, id) \/ RDelete(b, id)
    \/ EnableRemote /\ \E b \in OpBranches, nm \in {"p1", "q1"} : RCreate(b, nm)
    \/ \E b \in OpBranches, mode \in {"plain", "force", "theirs"} : Pull(b, mode)
    \/ \E b \in OpBranches : Push(b) \/ PushAbort(b)

Spec == Init /\ [][Next]_vars

StepBound == steps <= MaxSteps

(***************************************************************************)
(* Invariants I1..I12                                                      *)
(***************************************************************************)
TypeOK ==
    /\ ig \in BOOLEAN
    /\ used \subseteq Ids
    /\ \A b \in Branches, i \in Ids : rm[b][i].ex => rm[b][i].v \in V

\* I1: no two live remote configs on one branch descend from the same local
\* config instance (a double create).
I1_NoDoubleCreate ==
    \A b \in Branches, x, y \in Ids :
        (x # y /\ rm[b][x].ex /\ rm[b][y].ex) => rm[b][x].org # rm[b][y].org

\* I2: push only deletes a remote config whose local dir the user removed.
I2_DeleteOnlyUserRemoved == lastOp.i2
\* I2b: ... and only with --force (CLI help text, commands/sync.py:982-986).
I2b_DeleteNeedsForce == lastOp.i2b

\* I3: push never plans anything for an ignored component.
I3_IgnoredUntouched == lastOp.i3

\* I4: diff/push act only on entries of the ONE source tree (scope_manifest
\* partition) -- every tracked change refers to an in-tree entry.
I4_BranchIsolation ==
    \A b \in OpBranches : \A c \in Changes(St, b) :
        (c.trk /\ c.id # "none") => EntryTree(St, c.id) = Src(St, b)

\* I5 (generalized): a pull other than --theirs never destroys user work
\* (a file carrying edits that are on no remote), except the documented
\* ignored-component cleanup.
I5_PullKeepsLocalWork == lastOp.loss

\* I6: right after a successful push, a fresh diff of the same branch plans
\* nothing pushable and shows no drift on anything the push wrote.
I6_PushThenDiffClean ==
    lastOp.kind = "push" =>
        /\ Pushable(St, lastOp.b) = {}
        /\ \A w \in lastOp.wrote : KindOf(St, lastOp.b, w[2]) = "none"

\* I7: never-fetched entries are never planned as deletes.
I7_NeverFetchedNotDeleted == lastOp.i7

\* I8: every live manifest entry has its file in its own tree, unless it is
\* never-fetched or the user removed the dir.
I8_ManifestMatchesDisk ==
    \A id \in Live(St) :
        LET t == EntryTree(St, id) IN
        fl[t][mn[id].path].ex \/ mn[id].ph = NoHash \/ mn[id].path \in userDel[t]

\* I9: an ENCRYPTION_FAILED push leaves no remote write unrecorded in the
\* manifest (strong, atomic reading).
I9_AbortLeavesNoUnrecordedWrites == lastOp.kind = "push_abort" => lastOp.abortWrites = {}

\* I11: push never reverts a remote change with content the user never edited.
I11_NoLostUpdate == lastOp.lost
\* I11b: push never silently re-creates a tracked config deleted remotely.
I11b_NoSilentResurrect == lastOp.resur

\* I12 (S3 as safety): whenever diff says REMOTE MODIFIED, a plain
\* `sync pull` of that branch brings the config in sync.
I12_PullResolvesRemoteModified ==
    \A b \in OpBranches : PullPathsDistinct(St, b) =>
        \A id \in Ids : KindOf(St, b, id) = "remote_modified" =>
            KindOf(PullState(St, b, "plain"), b, id) # "remote_modified"
=============================================================================
