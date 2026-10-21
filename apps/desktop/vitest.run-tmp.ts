/**
 * One temp root per vitest run, removed at teardown.
 *
 * Tests mkdtemp under os.tmpdir() and not all of them remove what they make:
 * one run of the `electron` project left 54 dirs behind. Workers inherit
 * TMPDIR from this process, so everything a test creates lands here instead.
 * Keep the root prefix short: AF_UNIX tests nest more temp components under it, and TMPDIR may
 * already be a long profile-scratch path.
 */
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

export default function setup(): () => void {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'v-'))
  process.env.TMPDIR = root
  process.env.TEMP = root
  process.env.TMP = root

  return () => fs.rmSync(root, { recursive: true, force: true })
}
