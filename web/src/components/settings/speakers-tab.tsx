"use client";

/**
 * Settings → Meetings (tab id 'speakers'): the meeting models, voice fingerprints, names read from video,
 * and the cloud review of speaker names.
 */
import { AudioLines, Trash2 } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";
import { useApp } from "@/components/app-provider";
import { SectionTitle } from "@/components/settings/settings-dialog";
import { Button, Callout, ConfirmDialog, Switch } from "@/components/ui";
import { api } from "@/lib/api";
import { FramesNote, ModelSelect } from "@/components/meetings/model-select";
import { askNotificationPermission } from "@/lib/meeting-check";

/** The Meetings tab. */
export function SpeakersTab() {
  const { prefs, updatePrefs } = useApp();
  const [confirm, setConfirm] = useState(false);
  const [backfill, setBackfill] = useState<{ queued: { title: string }[]; skipped: { title: string; reason: string }[] } | null>(null);
  const [busy, setBusy] = useState(false);
  if (!prefs) return <div className="skeleton h-40" />;

  const runBackfill = async () => {
    setBusy(true);
    try {
      const result = await api.post<{ queued: { title: string }[]; skipped: { title: string; reason: string }[] }>("/api/meetings/voiceprints/backfill");
      setBackfill(result);
      toast.success(result.queued.length ? `Queued ${result.queued.length} meeting${result.queued.length === 1 ? "" : "s"}` : "Nothing to backfill");
    } catch (err) {
      toast.error("Couldn't start the backfill", { description: (err as Error).message });
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-6">
      <section className="space-y-3" data-testid="meeting-models">
        <SectionTitle title="Models" description="Defaults for new meetings; New meeting and each meeting's panels can pick others. Speech-to-text (Whisper large-v3-turbo) and speaker separation (pyannote 3.1) are fixed and run on this Mac." />
        <ModelSelect kind="speaker" id="pref-speaker-model" label="Speaker identification (reads the transcript, on this Mac)"
          defaultLabel="Recommended" value={prefs.speaker_model} onChange={(v) => updatePrefs({ speaker_model: v })}
          hint="Local models only: speaker names are always worked out on this Mac." />
        <FramesNote speakerModel={prefs.speaker_model} />
        <ModelSelect kind="frames" id="pref-vision-model" label="Video frames (names on the participants' tiles)"
          defaultLabel="Recommended" value={prefs.vision_model}
          onChange={(v) => updatePrefs({ vision_model: v })}
          hint="Only models that can read images are listed. Frames never leave this Mac." />
        <ModelSelect kind="summary" id="pref-summary-model" label="Meeting summaries"
          defaultLabel="Ask me each time" value={prefs.summary_model} onChange={(v) => updatePrefs({ summary_model: v })}
          hint="Summaries don't follow the chat model. With “Ask me each time” you pick the model when a summary is written; a cloud model also asks before a transcript is sent." />
      </section>

      <section className="space-y-1" data-testid="check-settings">
        <SectionTitle title="Meeting check" description="During a call (with the live transcript) or afterwards: conflicts with earlier meetings, things to raise with everyone, items still pending, and actions for you in your role (from Settings → Memory)." />
        <ModelSelect kind="summary" id="pref-check-model" label="Check model"
          defaultLabel="Local default (recommended)" value={prefs.check_model} onChange={(v) => updatePrefs({ check_model: v })}
          hint="A cloud model asks before the meeting is sent; a project set to local always uses a local model. With a local model, the live transcript pauses while the check runs." />
        <Switch id="pref-check-popups" checked={prefs.check_popups}
          onChange={async (v) => { if (v) await askNotificationPermission(); await updatePrefs({ check_popups: v }); }}
          label="Pop-up alerts during meetings"
          description="A macOS notification with only the number and kind of alerts, never their content. Switch it off if you share your screen in meetings: alerts then show only in the app. macOS also hides notifications while you share your screen unless you allowed them in System Settings → Notifications." />
        <div className="grid gap-3 py-2 sm:grid-cols-2">
          <label className="text-sm font-medium">Check automatically during a call
            <select value={prefs.check_auto_minutes} onChange={(e) => updatePrefs({ check_auto_minutes: Number(e.target.value) })}
                    className="mt-1 block h-9 w-full rounded-lg border border-border-strong bg-surface px-3 text-sm font-normal" data-testid="pref-check-auto">
              {[0, 5, 10, 15, 30].map((n) => <option key={n} value={n}>{n ? `Every ${n} minutes` : "Off (Check now only)"}</option>)}
            </select>
          </label>
          <label className="text-sm font-medium">Earlier meetings to compare with
            <select value={prefs.check_days} onChange={(e) => updatePrefs({ check_days: Number(e.target.value) })}
                    className="mt-1 block h-9 w-full rounded-lg border border-border-strong bg-surface px-3 text-sm font-normal">
              {[30, 90, 180, 365].map((n) => <option key={n} value={n}>Last {n} days</option>)}
            </select>
          </label>
        </div>
        <Switch id="pref-check-all" checked={prefs.check_all_projects} onChange={(v) => updatePrefs({ check_all_projects: v })}
          label="Include other projects' meetings"
          description="Off: meetings of the active project and meetings with the same people." />
      </section>

      <section>
        <SectionTitle title="Voice fingerprints" description="Biometric data, stored only on this Mac and never sent anywhere, exported or shown." />
        <Switch id="pref-voiceprints" checked={prefs.voiceprints} onChange={(v) => updatePrefs({ voiceprints: v })}
          label="Recognise people by their voice"
          description="Saves a voice fingerprint when you confirm a speaker's name, or when their name is on their video tile in 3+ turns. Later meetings (even audio-only) are then labelled by voice. Switching this off stops saving and matching; use the button below to delete what's stored." />
        <div className="mt-2 flex flex-wrap gap-2">
          <Button size="sm" onClick={runBackfill} loading={busy} disabled={!prefs.voiceprints || busy}>
            <AudioLines className="h-3.5 w-3.5" /> Make fingerprints for older meetings</Button>
          <Button size="sm" variant="ghost" onClick={() => setConfirm(true)}><Trash2 className="h-3.5 w-3.5" /> Forget all voices</Button>
        </div>
        {backfill && (
          <Callout className="mt-3" tone="info" data-testid="backfill-result">
            {backfill.queued.length} meeting{backfill.queued.length === 1 ? "" : "s"} queued in the transcriber (voice only, no new transcript).
            {backfill.skipped.length > 0 && <> Skipped: {backfill.skipped.map((s) => `${s.title} (${s.reason})`).join("; ")}.</>}
          </Callout>
        )}
      </section>

      <section>
        <SectionTitle title="Names on screen" description="For video recordings of Teams or Zoom calls. Read on this Mac after transcription." />
        <Switch id="pref-screen" checked={prefs.screen_names} onChange={(v) => updatePrefs({ screen_names: v })}
          label="Read participant names from the video"
          description="Finds the highlighted (active speaker) tile at each speaker's turns and reads its name label with Apple's on-device text recognition." />
        <p className="py-2 text-[13px] text-muted" data-testid="vision-review-note">
          The local vision model (above) then reviews frames of every speaker, always: it confirms or corrects what the
          text reader found and fills in names it missed, including frames with a shared screen or a side panel open.
          It only reads name labels, never faces, and frames never leave this Mac.
        </p>
        <div className="flex flex-wrap items-center gap-3 py-2 text-sm">
          <label htmlFor="pref-vision-frames">Frames the vision model reviews per meeting</label>
          <select id="pref-vision-frames" value={prefs.vision_frames ?? 0} data-testid="pref-vision-frames"
                  onChange={(e) => updatePrefs({ vision_frames: Number(e.target.value) })}
                  className="h-9 rounded-lg border border-border-strong bg-surface px-3 text-sm">
            <option value={0}>Auto: 4 per speaker (12 to 48)</option>
            {[...new Set([12, 24, 30, 36, 48, 72, 100, prefs.vision_frames || 12])].sort((a, b) => a - b)
              .map((n) => <option key={n} value={n}>{n}</option>)}
          </select>
          <span className="text-xs text-muted">about 6 to 9 s each</span>
        </div>
        <Switch id="pref-participants-scan" checked={prefs.participants_scan ?? true} onChange={(v) => updatePrefs({ participants_scan: v })}
          label="Read the participants list from the whole video"
          description="Checks one frame a second for the People / Participants panel (or the thumbnails strip while someone shares): it gives the meeting's attendees and a second check of who was speaking. Adds about 2 minutes per 15 minutes of video, on this Mac." />
        <Switch id="pref-screen-debug" checked={prefs.screen_debug} onChange={(v) => updatePrefs({ screen_debug: v })}
          label="Keep frame crops (debugging)"
          description="Saves the tile crops next to the transcripts so you can see what was read. Off by default." />
      </section>

      <section>
        <SectionTitle title="Cloud review" description="Speaker names are always identified on this Mac first." />
        <Switch id="pref-cloud-review" checked={prefs.speaker_cloud_review} onChange={(v) => updatePrefs({ speaker_cloud_review: v })}
          label="Let the selected cloud model review speaker names"
          description="When a cloud model is selected, it gets the transcript, the local suggestions, names seen on screen and the people from memory relevant to this meeting. It may change names you didn't set yourself; you see every change and can keep or revert it. Voice fingerprints and video frames are never sent. In Auto mode you're asked first; confidential projects and chats skip it." />
      </section>

      <ConfirmDialog open={confirm} onOpenChange={setConfirm} title="Forget all voices?" confirmLabel="Forget all"
        description="Every stored voice fingerprint is deleted. Names, roles and facts stay."
        onConfirm={async () => { const r = await api.del<{ deleted: number }>("/api/voiceprints"); toast.success(`${r.deleted} fingerprint${r.deleted === 1 ? "" : "s"} deleted`); }} />
    </div>
  );
}
