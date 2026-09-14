/**
 * Capture the demo for the blog post: a still of the feature panel, and a video of a
 * correction landing.
 *
 *   bun install && bunx playwright install chromium  # once
 *   bun bin/record-correction.ts                     # -> docs/features.png, docs/correction.webm
 *
 * Drives the real UI against the real stack, so it needs `docker compose up -d` and a
 * seeded node with chargebacks still in flight. Confirming is a real write and system
 * time only moves forward. To reproduce the same case, reset this demo's volumes
 * deliberately (see README.md), then run `./bin/seed.sh` against the empty database.
 */

import { chromium, type Page } from 'playwright'
import { mkdirSync, renameSync, readdirSync, rmSync } from 'node:fs'
import { join } from 'node:path'

const UI = process.env.UI_URL ?? 'http://localhost:5173'
const OUT = process.env.OUT_DIR ?? 'docs'
const SIZE = { width: 1280, height: 800 }

const beat = (ms: number) => new Promise((r) => setTimeout(r, ms))

const correctionTab = (p: Page) => p.getByRole('button', { name: 'Correction', exact: true })
/** Ranked pending corrections, highest impact first. Scoped to the dock: an unscoped
 *  match can latch onto a hidden element elsewhere and then wait for it forever. */
const topCase = (p: Page) =>
  p.locator('aside').last().getByRole('button', { name: /→/ }).first()
const confirmBtn = (p: Page) => p.getByRole('button', { name: /Confirm chargeback/i }).first()

async function main() {
  mkdirSync(OUT, { recursive: true })
  const browser = await chromium.launch()
  const ctx = await browser.newContext({
    viewport: SIZE,
    deviceScaleFactor: 2,
    recordVideo: { dir: OUT, size: SIZE },
  })
  const page = await ctx.newPage()
  const recordingFrom = Date.now()          // the video timeline starts here
  await page.goto(UI, { waitUntil: 'networkidle' })
  await beat(2500)

  // The still: any transaction will do, and clicking a row is what populates the
  // feature panel. Do it first, because it also seeds the correction tab with that
  // account, and an account with no chargebacks in flight has nothing to confirm.
  await page.locator('tbody tr').first().click()
  await page.getByText(/each one is a query/i).first()
    .waitFor({ state: 'visible', timeout: 30_000 })
  await beat(1000)
  await page.locator('aside').last().screenshot({ path: join(OUT, 'features.png') })
  console.log(`wrote ${OUT}/features.png`)

  // Reload to clear that selection: with nothing selected the correction tab ranks
  // every pending chargeback by how much confirming it would move a real score.
  await page.reload({ waitUntil: 'networkidle' })
  await beat(1500)
  await correctionTab(page).click()

  // The ranking is cached, so a case can name an account whose chargebacks have since
  // been confirmed. Walk down the list until one still has something in flight.
  const cases = page.locator('aside').last().getByRole('button', { name: /→/ })
  await cases.first().waitFor({ state: 'visible', timeout: 60_000 })
  const confirm = confirmBtn(page)
  let picked = ''
  for (let i = 0; i < Math.min(6, await cases.count()); i++) {
    const c = cases.nth(i)
    picked = (await c.innerText()).replace(/\n/g, '  ')
    await c.click()
    await beat(900)
    if (await confirm.isVisible().catch(() => false)) break
    console.log(`skipping ${picked}: nothing in flight`)
    picked = ''
  }
  if (!picked) throw new Error('no ranked case still has a chargeback in flight')
  console.log(`subject: ${picked}`)
  // Everything above is setup and none of it is worth watching. Note where we are on
  // the video timeline so the head can be cut, keeping a beat before the click.
  const actionAt = Date.now()
  await beat(2500)                    // hold on the pending state
  await confirm.click()

  await page.getByText(/reproduced/i).first().waitFor({ state: 'visible', timeout: 60_000 })
  await beat(4500)                    // hold on before -> after -> reproduced

  // Report what actually got filmed. The subject is whichever correction ranked highest
  // at record time, so it changes between runs — anything quoting these numbers in prose
  // has to be re-synced against this output.
  const dock = await page.locator('aside').last().innerText()
  const scores = [...dock.matchAll(/^0\.\d{3}$/gm)].map((m) => m[0])
  const summary = dock.split('\n').find((l) => /^Confirmed \d+ chargeback/.test(l))
  console.log(`\nfilmed: ${scores.slice(0, 3).join('  ->  ')}`)
  if (summary) console.log(summary.replace(/\s+/g, ' '))

  await ctx.close()                   // flushes the video
  await browser.close()

  const raw = readdirSync(OUT).filter((f) => f.endsWith('.webm')).sort().pop()
  if (!raw) throw new Error('playwright wrote no video')
  renameSync(join(OUT, raw), join(OUT, 'correction.raw.webm'))

  const skip = Math.max(0, (actionAt - recordingFrom) / 1000 - 1.5)
  const ff = Bun.spawnSync(['ffmpeg', '-v', 'error', '-y',
    '-ss', skip.toFixed(2), '-i', join(OUT, 'correction.raw.webm'),
    '-c:v', 'libvpx-vp9', '-crf', '32', '-b:v', '0',
    join(OUT, 'correction.webm')])
  if (ff.exitCode !== 0) throw new Error(new TextDecoder().decode(ff.stderr))
  rmSync(join(OUT, 'correction.raw.webm'))
  console.log(`wrote ${OUT}/correction.webm (cut ${skip.toFixed(1)}s of setup)`)
}

main().catch((e) => { console.error(e); process.exit(1) })
