GAME MODEL EXPORTER
===================
Replaces one of the game's models with your own .blend. Your objects and materials
can be named anything, and there can be 3 pieces or 3,000.

YOU NEED
  Blender, and Python 3 for Windows from python.org (then double-click the .pyw).
  The game's BlendMeshSpec.zig (see SPEC FILE).

SPEC FILE
  The tool reads and writes the game's files using BlendMeshSpec.zig, the format description that
  Senders' developer generates ("zig build export-spec"). That file belongs to the developer, so it is
  NOT included here. Get it from the developer, then on the Spec tab click "Add spec" and pick it: a copy is
  kept in %APPDATA%\GameModelExporter\specs, where the map editor and the Blender add-on find it too
  (or drop it in this folder's specs\ folder). Each game build may need its matching file.

LICENCE
  The tool's own code is open source under the MIT licence (LICENSE). It contains no game files or game
  data; it works only on the copies of the game you own.

HOW TO USE
  1. Your model        -> your .blend
  2. Game model to     -> the game's own file you want to replace,
     replace              e.g. ...\bike-4stroke.blend.bin
  3. Export.

  The game file tells the tool everything else: which model it is, which file version
  the game uses, and where every part, pivot and mount point belongs. It is replaced
  with your model, and the original is kept next to it as bike-4stroke.blend.bin.orig
  (always used as the template afterwards; rename it back to undo).
  Tick "Don't replace it" to write the result somewhere else instead.

BIKES FROM MX BIKES AND MX SIMULATOR (button "Import MX Bikes / MX Sim bike..." in step 1)
  Pick an MX Bikes model.edf (the loose one in a bike mod's folder) or an MX Simulator bike .saf.
  The bike is assembled the way the game puts it together at runtime and saved as a .blend in
  Documents\GameModelExporter\imports\<bike>, which becomes "Your model":
  - MX Bikes: the chassis, steering (tilted to the 26 degree steering angle around the steering head),
    front suspension (slid along the fork until the front axle is at tyre height) and swingarm (swung
    about its pivot until the rear axle is at tyre height). Wheels are not part of MX Bikes bike models -
    the game's own wheels are used.
  - MX Simulator: a bike .saf is always several parts (frame, fork_upper, fork_lower, swingarm); they are
    put together on the shared MX Simulator chassis - axles on the game bike's axles, the fork tubes on
    the steering axis, the frame over the game bike's frame. A .saf with several bikes asks which one,
    and which skin. MX Simulator units are feet - converted to metres.
  The parts arrive named after the game's parts (frame, handlebars.top = steering / fork tops,
  handlebars.bottom = fork lowers, swingarm), so they always land in the right place.
  Not read: scrambled MX Simulator files (.scram) and encrypted MX Bikes .pkz archives - those are
  protected by their authors. A bike whose skin is scrambled comes in untextured.
  Step 2 must be set before importing an MX Simulator bike (it is built around the game bike).

EDIT MODEL (3D): LINE UP · PAINT · PARTS
  One window with three tabs. Line up = placing the bike on the game's (below). Paint and Parts work on
  bikes imported from MX Bikes / MX Simulator ("Paint & parts..." in step 1 opens it on the Paint tab).
  PAINT - works like a racing game's livery editor:
  - Click the bike to pick a spot, then "+ Shape" (rectangle, circle, ring, star, stripe, chevron, number
    plate...), "+ Logo / PNG..." (or drop a PNG on the bike) or "+ Text..." (any installed font).
  - Move tool (V): drag a design anywhere over the bike - it follows the surface, also onto other parts.
    Corner squares size it, the blue knob turns it (Shift = 15 degree steps). Wheel = size, Shift+wheel =
    turn, Ctrl+wheel = zoom the view. Arrows nudge, [ ] turn, + - size, Ctrl+D duplicate, Delete.
  - Colour: the colour square on the right changes the selected design as you drag (recent colours are
    kept). Eyedropper (I): click the bike or the texture to take its colour - the selected design gets it at
    once. Colour bucket (B): click a piece to paint it the current colour ("keeps the texture's detail" tints
    it so graphics and shading stay; "one panel" paints just the patch of texture you click).
  - "Stay on its piece" keeps a design from spilling onto other parts that share the texture.
    "Copy to other side" puts a twin on the bike's other side, reading the right way round.
  - The design list is the stacking order (top = in front). Ctrl+Z / Ctrl+Y undo / redo everything.
  - "Start from": an MX Bikes paint (.pnt) of the bike (Documents\PiBoSo\MX Bikes\mods\bikes\<bike>\
    paints, or any .pnt), one plain colour, or the bike's own texture. Paints saved by newer paint tools are
    protected (encrypted) and can't be used; a paint made for another version of the bike won't line up.
  PARTS - click pieces (Shift+click for more) and "Delete picked pieces" (they can be restored); "Add a
    part..." brings in an .obj, MX Simulator .jm or MX Bikes .edf at the picked spot - move / turn / size
    it there and choose the game part it moves with.
  "Apply to the model" paints the textures at full size and rebuilds the bike; going back to Line up offers
  it and then reloads the view. Your line-up is kept. Work is saved as you go (workshop.json).

BIG PARTS (Edit model 3D)
  An imported bike is a few big parts cut into many loose pieces. "Clicking in the view picks: a big part"
  (on for imported bikes) selects the whole part you click - chassis/frame, steering (handlebars.top),
  front suspension / fork lower (handlebars.bottom), swingarm - and the buttons under it pick one directly.
  Moves and turns then go around the game part's own pivot (steering head, swingarm pivot...).
  Switch to "a loose piece" to move single pieces as before.

AUTO ALIGN (Edit model 3D)
  "Auto align (axles, pegs, bars)" finds on your model:
    the front axle (where the fork meets the front tyre) and rear axle (where the swingarm meets the rear
    tyre) - from your wheels, or, with no wheels, from the bottom of the fork lowers and the back of the
    swingarm; the foot pegs (the widest points low down by the swingarm pivot); the handlebar ends.
  Then: the whole model gets one size and pitch for the best overall fit, the frame slides so the pegs
  sit on the game's peg mounts, the handlebars so the grips sit on the game's bars, the fork lowers
  turn about the front axle to meet the fork tops and the swingarm about the rear axle to meet its
  pivot. Green crosses = the game bike's spots, pink rings = yours. Undo puts back what you had.

LINE-UP PRESETS
  Edit model > "Save as preset..." keeps your line-up (fit, part moves, materials) under a name, in
  %APPDATA%\GameModelExporter\lineup_presets. Step 1 > "Line-up preset" uses it for models that have no
  saved line-up yet; bikes imported from MX Bikes / MX Simulator pick the newest preset made for that
  game automatically, so one careful line-up does every bike from it. "Use a preset..." applies one in
  the editor.

WHAT HAPPENS TO YOUR MODEL
  - The game's own wheels (back_tire, front_tire) are always kept - your model
    doesn't need wheels. Anything of yours sitting where the wheels are is left out.
  - Your model is lined up with the original bike by shape: every right-angle turn is
    tried (so it can be lying on its side, facing backwards or upside down), then the
    fit is refined so your surfaces lie on the original's (position, turn and size).
  - Every object is split into its loose parts and each part goes to the game part in
    that space: swingarm, forks, handlebars, suspension, frame. So the swingarm moves
    with the suspension and the handlebars steer.
  - The game's pivot points are kept.
  - "Mirror my model left <-> right" (Options) flips it across the bike's centreline after
    lining it up - for models that come in mirrored (exhaust on the wrong side). Faces are
    turned so they still face outward, and your alignment edits are kept.
  - Your materials go into each game part's material slots by closest look.
  - Mount points, collision capsules, camera spot and lattices come from the original.
  - Corners that sit at the same spot are welded, as the game's own exporter does, so the
    game shades your model smoothly (models ripped from other games are often "unwelded" -
    every triangle separate - which looked faceted in game).
  - Shine (metallic / roughness) comes from your roughness/metallic textures when you use
    them; the unused slider underneath is ignored, as in Blender.
  - Subdivision Surface modifiers are applied into the mesh (the original bike is fully
    modelled, ~319,000 vertices, and never uses the game's subdivision). The log shows your
    vertex count next to the original's.
  - Open, thin parts (a single sheet of faces) get a back side, because the game doesn't draw
    the back of a face.
  - The game's "plastic.style" alternatives are left empty; your bodywork stays in the frame.
  - Cameras, lights and other extras are left out.

EDIT MODEL (3D)
  Draws your model on your graphics card like Blender's viewport: full detail, smooth
  lighting, anti-aliased, with the original bike as a see-through blue-grey ghost and the
  game's kept parts (the wheels) solid. "As in game" (on by default) shows exactly the texture
  / colours the game will get; untick it to colour your pieces by game part.
  "As in game" uses the game's own material and lighting rules: texture x colour, metallic is
  on/off (0.5 and up = metal, softened by roughness), the game's highlight and reflection model
  and its AgX tone mapping. Its lighting and colour settings are read live from your game
  folder (design\Render.zon) - nothing is copied. Sky, shadows and ambient occlusion are
  approximated, so judge colours and shine, not exact brightness.
  First time: say yes to installing numpy, Pillow, PyOpenGL and pyopengltk (free libraries).
  (This is only the tool's viewer; the game itself keeps using Vulkan. If OpenGL can't start
  on your PC, the editor falls back to a slower software view.)
    View    left-drag orbit · right-drag pan · wheel zoom · Side/Front/Top/Back (keys 1-4)
    Select  click a piece · Shift+click add/remove · Shift+drag a box · Esc / click empty = clear
            "Whole object" / "Whole part" grow the selection
            Shift/Ctrl+click in the Game parts list to pick several parts
    Move    Ctrl+drag slides the selection · arrows, PgUp/PgDn nudge · [ ] turn · + / - scale
            or use the Move / Turn / Scale boxes (+/- buttons use the Step sizes)
  Align mode
    - With game parts selected: moves/turns/scales each around its pivot (axle, hinge...);
      "Snap to original" lines a part up with the original's part;
      "Use the game's original for this part" keeps the game's part instead of yours.
    - With pieces selected: moves just those pieces (around their centre).
    - "Move them to game part" (left) sorts the selected pieces into another game part.
  Materials mode
    - The list holds the original bike's materials (Seat, Chrome, Dark Metal, ...).
      Select pieces, pick a material, "Apply to selected pieces". "(my own material)" undoes it.
    - "Select pieces with this material" selects everything using it.
  Save, then Export. Everything is kept in yourfile.blend.gamemap.json.

TEXTURES
  The game can only load the textures on its own list (Spec tab) - it can't load new files.
  "Bring my textures" works around that: all your textures (and the colours of untextured
  materials on the same parts) are packed into one image that replaces one of the game's
  texture files - by default crashpad.png, which only the bike uses - and your UVs are
  remapped onto it. The original texture is kept as crashpad.png.orig.
    - The texture file is found next to the game's model files; if not, Browse to it.
    - Your textures are packed at high resolution (up to 4096 x 4096 in total); the game loads
      each texture at its own size, so the replacement doesn't need crashpad's 630 x 630.
    - Textures Blender can't find (pink in Blender) can't be brought over - those parts get
      their material colour. The log says which ("could not be loaded (missing file?)").
    - Anything else in the game that uses that texture will show your image too.
    - Transparency is removed by default: "Masked" textures keep a mask in the alpha channel,
      which the game draws as see-through. Tick "Keep transparency" only for real cut-outs.
    - Materials that do more than plug an image in (paint masks, mixes, tints, colour ramps -
      e.g. "Masked" textures painted through their alpha) are rendered by Blender itself, so
      the game gets the colours you see in Blender. This takes a little longer.
    - Pieces you give a game material (Edit model > Materials) use that instead.

FINISH
  "Finish of my parts" (Options) sets how shiny your parts are in game: Satin (default),
  Matte or Glossy. The game reflects its surroundings strongly on shiny / metallic surfaces,
  and shine values from ripped models rarely suit it. "From my Blender materials" uses your
  own roughness / metallic.

NORMAL MAPS
  The original bike uses no normal maps at all - every part is a plain colour on real
  geometry - and the game's only two normal maps are the tree bark and the rider's cloth.
  So "Bake my normal maps into the colours" (on by default) draws your normal map's bumps
  into the texture as light and shade. Real normal maps on the bike need the dev to add a
  bike normal texture to the game. (Advanced: gamemodel_blender.py --normal-slot/--normal-file
  can take over one of the two existing slots, which breaks the trees or the rider.)

MAP EDITOR (button "Map editor (heightmap studio)" at the bottom right of the main window)
  The game's whole map, edited in place with heightmap-studio tools, with a live 3D view of the spot you work
  on (the surrounding ground is drawn around it). The game's map file is only read.
  Brushes only redraw what they touch (2D and 3D), panning slides the view at once, and quick wheel turns
  or drags are merged into one redraw - so sculpting, painting and moving around stay smooth on big areas.
  EDIT IN 3D  Sculpt, Surface, Nature, Track, Jumps and Ruts work in the 3D view too: left-drag on the terrain
    paints / sculpts / moves points exactly as on the map (the brush ring shows where), right-drag turns the
    view, middle- or Shift+right-drag moves it, wheel zooms. Tracks, their points, jumps and ruts are drawn
    on the terrain. On the other tabs left-drag turns the 3D view as before.
  ONE DIRECTION  The 2D map and the 3D view always face the same way: turn the 3D view and the map turns
    with it (what is ahead in 3D is up on the map), and the other way round - Ctrl+right-drag on the map
    turns it, Q / E turn it 15 degrees, the north arrow (top right of the map) puts north back up.
  WORK AREAS  Just start editing anywhere (Track, Jumps, Ruts, Sculpt or Surface tab): a work area of the real
    ground is made there - 256 m at 25 cm per pixel by default (Map tab: "New areas") - and it grows when a
    track or brush goes past its edge (up to 2049 pixels a side, e.g. 512 m at 25 cm). Areas never overlap;
    click one to switch to it. Map tab: Size..., Rename..., Remove, "Import a heightmap image as an area..."
    (on the game map's scale - black = its lowest point, white = its highest - or sitting on the ground with
    your own black->white metres; 16-bit PNG recommended), "Blend areas into the map over" (m).
  FLATTEN THE MAP  Map tab > Whole map: "Flatten the whole map" + Apply turns everything except the game
    tracks you tick (1, 2...) into one flat plane - at "auto" (the height of those tracks) or a height you type -
    easing into the kept tracks over the distance given; "no trees / rocks on it" clears them too. Your areas
    sit on the plane. Un-tick + Apply brings the game's ground back. It is kept with your work and written into
    the map when you save. (The editor always builds on the game's original map, even after Install, so
    saving again never stacks your old changes twice.)
  OVERLAPPING AREAS  "Areas may overlap" (on): import / move / grow areas over each other. Where they overlap
    the one in front wins - "Bring to front" / "Send to back" (the list is in back-to-front order).
  IMPORTED TRACK SETTINGS  Map tab: "raise (+) / sink (-)" moves an imported track up or down (sink it so its
    border walls don't stand out, e.g. Temecula Creek); "level pad" keeps its exact shape on a level pad,
    "fade in over N m" blends it into the map from its edges inwards (no walls at its border). Apply.
    Sculpt tab: "Erase import" brushes parts of the imported track away (the ground shows there instead, shown
    blue while you paint), "Restore import" brings them back.
  TINTS PER TRACK  The game has 8 ground surfaces (no new textures can be added), but it blends two of them
    in every 0.5 m cell. Surface tab, "Mix in [surface] at [%]": everything you paint gets the picked layer
    blended with that one by that amount - e.g. Light dirt with 20% Grass on one track, Track dirt 1 with 30%
    Track dirt 2 on another. Each spot keeps its own mix; the editor shows it. Riding follows the blend too.
  ORIENTATION  The map view (2D and 3D) shows the game's world the way the game does (it used to be a mirror
    image). MX Bikes tracks come in exactly as they are (both games' worlds have the same handedness).
    Areas imported by the version that turned tracks over are turned back automatically when the editor
    opens, with everything made in them. "Turn 90°" turns clockwise as you see it on the map.
  SAFETY  "Save map" won't write inside the game's folder, and writes through a temporary file that is
    only given its name once it reads back complete. "Install a saved map" checks the file first (not empty,
    same game version, reads as a whole map) and swaps it in only after a complete copy - if anything goes
    wrong the game's map stays as it was. If the game's map.blend.bin is ever empty or damaged (the game says
    "Loading game assets failed: EndOfStream"), opening the map editor offers to put the .orig backup back,
    or tells you to use Steam's "Verify integrity of game files".
  MX SIMULATOR TRACKS  .saf tracks (and unpacked track folders, or their terrain.hf) work like MX Bikes
    tracks: drag one onto the map, use Other…, or pick it in the list - the list also shows the .saf tracks
    in the "MX Simulator folder" (C:\Program Files (x86)\MX Simulator unless you pick another). Nothing
    has to be unpacked: the tool reads terrain.hf / terrain.png (and timing_gates, map.png) straight out of
    the .saf. ("Unpack a .saf…" unpacks one anyway if you want the files.) .saf files that hold a bike or
    gear instead of a track are skipped; one holding several tracks asks which. MX Simulator measures in
    feet - it is converted to metres. Only the part around the track (its gates and checkpoints, plus
    60 m) is taken. The import window shows the terrain as it will sit in Senders next to the track's own
    map picture: if one is the mirror image of the other, tick "Mirror it".
    "Size %" in the import window (MX Bikes and MX Simulator tracks) scales a track evenly - heights too -
    if it feels too big or small next to Senders' bike (100 = its true size).
    MX Simulator's bikes have true-size wheels (front 0.70 m, rear 0.665 m); Senders' bike is about 4%
    bigger (0.725 / 0.69 m), so "Match the MX Sim bike (104%)" keeps every jump the same size next to the
    bike as it is in MX Simulator. (MX Simulator's wide default field of view - 126 degrees in your settings
    against Senders' 82-105 - also makes the same jump look smaller there.)
  MX BIKES TRACKS  Drag a track's .pkz (or its unpacked folder, or its .trh) from Explorer onto the map
    editor, or pick one in the Map tab's "MX Bikes tracks" list (it lists Documents\PiBoSo\MX Bikes\mods\
    tracks; "Tracks folder: Change…" picks another folder if MX Bikes is elsewhere on your PC, "Default"
    goes back) and press Import. Its terrain heightmap becomes a work area at its real size, sitting on the
    ground in the middle of your view (you can turn / mirror it and raise it), blended in over 15 m. Then
    every tool works on it. Tracks whose author protected (encrypted) the .pkz can't be read - the list marks
    them "(protected)". Only the heights come over (MX Bikes stores them as signed numbers - versions before
    this one read them wrongly, which made tall walls and flattened the hills; areas imported with an older
    version are repaired automatically when the map editor opens).
    The track keeps its exact shape on a level pad (no hills of the map under it); around its edges it meets
    the map over 25 m. After importing it is in "placing" mode (orange box): drag it where it should go,
    "Turn 90°" (or R) if needed, then "Confirm placement" (or Enter; Esc stops moving). "Track surface (material)"
    (the box at the top of the import window, and in the Map tab) gives the entire imported track one of the game's surfaces
    at once - paint over parts of it in the Surface tab. "Clear rocks & trees under it all" (on) removes them over
    the whole area when the map is saved. "Move this area…" puts any area back into placing mode later -
    everything made in it moves along and it re-seats on the ground at the new spot.
  Terrain   generated hills (seeded presets: flat pad, gentle slope, rolling hills, rough, ridges; height,
            feature size, roughness, detail, slope) added on top of the game's ground, fading out at the
            area's edges. A reference image (map / sketch / photo) can be shown over the area for tracing.
  Track     click to add points, drag to move, click the line to insert; Shift = 45 degree steps;
            Shift+click a second point selects a stretch (set its banking); right-click an end point
            for a 45 / 90 / 180 degree turn (lane gap for hairpins). Per point: width, banking.
            Per track: cut depth, height smoothing, how much it follows the ground, blend edge, raised
            edges, surface, closed loop, start funnel. Several tracks per area.
  Jumps     library: tabletop, double, triple, step-up, step-down, kicker, roller, whoops, rhythm,
            berm wall, drop-off, gap - plus your own (template editor: drag the profile's points).
            Pick one in the list and click a track, or drag it from the list onto a track. On the map:
            drag a jump along its track, Ctrl+drag copies, Shift+drag changes the length. Snap distance,
            repeat N times every M m. Each jump: position, length, height, width, sideways, angle, side blend.
            "This jump's shape": drag the selected jump's profile points and the jump changes as you drag
            (double-click adds a point, right-click removes one, "Back to the template's shape" undoes it).
            "Save as template (shape + sizes)" keeps the shape AND the length, height, width, side blend, angle
            and sideways - new jumps made from that template start with those sizes (the template editor
            has the same size fields).
  Ruts      pen or freehand; 1-6 ruts side by side, depth, width, sharpness, berm height / width / side.
            Bumps: single, scattered, or braking bumps (rows across the track, growing towards the corner).
  Sculpt    Raise, Lower, Smooth, Flatten, Flatten slope, Ramp, Noise, Erode (rain-drop hydraulic erosion),
            Clone (Alt+click the source), Terrace, Erase - on the final ground or the terrain only (tracks
            then follow it). Mask / Unmask hide the track features where painted. [ and ] change the radius.
  Surface   paint the game's ground layers (tracks get theirs automatically - "Track dirt 1" is the game's
            own track surface) with a palette of brush tips: hard / soft / rough-edged round, speckle,
            stipple, spatter, clumps, splotches, grunge, chalk, bristles, dry brush, cross-hatch, leaves.
            Opacity and density make sparse cover that doesn't fill in when you go over it again, texture
            scale sets the size of the dots / patches, the texture can follow the stroke. Example - grass with
            dirt patches: Grass with Hard round, then Light dirt with Splotches or Speckle at 0.3-0.5.
            Where a game cell (0.5 m) gets several layers, it is saved as the game's own blend of the two
            most common. Export surface mask saves it as a transparent texture-mask PNG.
  Nature    trees, bushes, grass clumps and rocks: pick what (big trees, small trees & bushes, bushes &
            forest grass, bushes only, meadow grass clumps, large / small boulders, small rocks, clear, back
            to automatic). "Bushes only" keeps the ground you have (e.g. grass) and puts just a speck of forest
            floor (about one 0.5 m ground cell) under each bush - the game only grows bushes on forest floor -
            with a low size level so there's no room for trees; the brush Density sets how many bushes.
            and click to place spots one at a time, or paint them with any brush tip (Ctrl = back to
            automatic). The game grows these itself from the map: the ground layer picks the kind (forest
            floor = woods, grass = meadow, rock = small rocks), a tree-height map says how tall the woods
            may grow there (255 = like its deep forest, 30-100 like its forest edges) and a boulder map says
            where boulders lie. So a spot gets one or a few - exactly which and where is the game's choice.
            The map shows what will grow (crowns, boulders, pebbles), including the game's own trees.
  Layers    every track, jump, rut and bump: show / hide, rename, group, reorder, delete.
  Undo / redo (per area), presets, Export heightmap (16-bit PNG of the area). Your work is saved
  automatically and offered again next time; "Save work as..." / "Open work..." keep copies (.hmworld).
  "Save work" only keeps your editing - it does NOT write the game map. "Save map for the game" does.
  2D view: right- or middle-drag moves, wheel zooms; blue dashes = the game's tracks, red ring = your last
  ride, "Go to" buttons jump there. 3D view: left-drag orbits, right-drag pans, wheel zooms, double-click
  centres; height exaggeration, slope shading (steep = red) and a 10 m grid are display-only.
  TO THE GAME  "Save map for the game" (top bar, or the Map tab's "Save map") writes every area into a copy
    of the game's map, map.blend.bin, in the folder shown at "Maps are saved in" (Downloads unless you
    pick another with "Change…"; "Save map as…" asks for a name). When it's done it shows the full path
    and time and offers to install it straight away. The copy has (heights, a
    detail clip of up to 12.5 cm, the surfaces, rocks and trees cleared where the ground changed or got a
    surface), then "Install a saved map..." (the game's map is kept as map.blend.bin.orig; "Restore
    original" puts it back; Steam "verify files" does too).
  If something goes wrong, the error is shown and written to studio_errors.log in
  %APPDATA%\GameModelExporter - send me that file.

REVIEW MATCHING (optional)
  Shows every game part, which of your parts went into it, and why. Select a game part:
    Put object(s) here...  /  Leave empty  /  Use the original  /  Match again
  "Save matching" stores it next to your .blend (yourfile.blend.gamemap.json) and
  Export uses it. If you change the .blend afterwards it is matched again.

IN THE LOG
  "N% of your surface lies on the original's"  how well the automatic fit matched; if
                                           it is low, use Align parts
  "will all show as ..."                    several of your materials share one slot
  "texture ... is not one the game knows"   the game can only load the textures listed
                                            on the Spec tab; others are left off and the
                                            material colour is used

OTHER TABS
  Spec: models & nodes   what the game expects (picked automatically from the game file)
  Inspect / Convert      open any .blend.bin / _blend.json, check it, convert

FILES
  Game Model Exporter.pyw  the app        gamemodel_blender.py  runs inside Blender
  automap.py   matching                   zigspec.py            reads BlendMeshSpec.zig
  align_editor.py          the 3D editor   gl_view.py   its graphics-card view
  world_editor.py          the map editor  terrain_io.py reads/writes the map's terrain
  hm_studio.py  hm_core.py  hm_gl.py  map_editor.py     its tools / engine / 3D view / helpers
  mxb_io.py                reads MX Bikes track terrains (.pkz / .trh)
  mxs_io.py                reads MX Simulator tracks (.saf / terrain.hf + terrain.png)
  bike_import.py  edf_io.py  imports MX Simulator (.saf / .jm) and MX Bikes (.edf) bikes
  autoalign.py             Auto align       lineup_presets.py   line-up presets
  workshop.py  workshop_gl.py  livery.py   Paint & Parts (panel / 3D view / painting engine)
  pnt_io.py                reads MX Bikes paints (.pnt)
  specs\                   empty - see SPEC FILE below
  LICENSE                  MIT licence for this tool's code    THIRD_PARTY_NOTICES.txt  libraries it uses
  gamemodel_addon.zip      same exporter as a Blender add-on (File > Export > Game Model;
                           set "Original game file" in the export dialog)

GAME UPDATES
  When Steam updates the game, its files come back as the new originals (your bike and map mods are
  replaced). The tool notices that an old .orig backup is from an older game build and uses the
  game's new file instead - just export / install again. Newer builds store normals in the model:
  your model's smoothing is exported (corners at the same spot are smoothed, creases over ~70
  degrees stay sharp).
  The version number at the start of every game file is a hash of the whole spec - including the lists of
  part names - so almost any update changes it, even a lighting fix. When no spec has the new number, the
  tool checks that every game file of that version still reads exactly with a spec it has; if so it makes a
  matching spec itself (BlendMeshSpec.auto-<number>.zig in %APPDATA%\GameModelExporter\specs, part lists
  taken from the game's own files) and carries on. Only a real change in how files are laid out still needs
  the developer's new BlendMeshSpec.zig.

LIMITS
  - Only the game's own models can be replaced; new ones need the dev.
  - Not verified on real game data: normals (newer builds), armatures, linked meshes.
