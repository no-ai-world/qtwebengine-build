@echo off
setlocal EnableExtensions
rem ============================================================================
rem Build QtWebEngine from source on Windows / MSVC 2022 x64 with proprietary
rem codecs (H.264 + AAC + MP3) enabled, optionally with ccache so a build that
rem does not fit inside one CI job can be continued in the next job.
rem
rem Step by step this follows the reference guide WebEngineMP4Build_6.8.3.md
rem (repo: open-prison-education/ope-lms):
rem   prepare env -> fetch sources + submodule -> patches -> qt-configure-module
rem   -webengine-proprietary-codecs -> build -> install -> package runtime
rem
rem PHASES. The first argument picks how much to do, so a long build can be cut
rem at a time budget and continued later:
rem   build.cmd all       prepare + build + finish       (default, local one-shot)
rem   build.cmd prepare   env, source, patches, configure   (idempotent)
rem   build.cmd build     cmake --build only, records STATE_FILE
rem   build.cmd finish    install + package
rem
rem STATE_FILE (default %WORK_ROOT%\build-state.txt) gets "ok" or "failed <code>"
rem when the build phase ends - "failed" also covers a phase that stopped before
rem cmake ever ran (bad environment, missing tools, no configure). The file is
rem deleted only just before cmake starts, so its ABSENCE after a killed build
rem means "ran out of time, resume next round" - that is how the caller tells a
rem time-out apart from a genuinely broken build, and so avoids looping forever
rem on the latter.
rem
rem ENCODING: this file is deliberately ASCII-only. cmd.exe decodes a batch file
rem with the console code page, so non-ASCII bytes here corrupt the parser.
rem CRLF is enforced via .gitattributes. Two batch rules are also followed:
rem no literal parentheses in echo lines inside an if-block (an unescaped ) would
rem close the block), and interpolated paths in such blocks are quoted.
rem
rem Inputs (all via environment variables):
rem   QT_PATH           required. Qt msvc2022_64 prefix (has bin\qt-configure-module.bat)
rem   WORK_ROOT         default %SystemDrive%\qtwebengine-work (CI: biggest disk)
rem   SRC_DIR           default %WORK_ROOT%\src\qtwebengine
rem   BUILD_DIR         default %WORK_ROOT%\build
rem   INSTALL_PREFIX    default %WORK_ROOT%\install
rem   DIST_DIR          default %WORK_ROOT%\dist
rem   TOOLS_BIN         default %WORK_ROOT%\tools\bin  (bison/flex/ccache live here)
rem   QT_TOOLS_DIR      default %SystemDrive%\Qt\Tools   (Qt's Ninja)
rem   STATE_FILE        default %WORK_ROOT%\build-state.txt
rem   QT_VERSION        default 6.8.3
rem   QTWEBENGINE_REF   default QT_VERSION
rem   QT_SOURCE_URL     default https://github.com/qt/qtwebengine.git
rem   BUILD_TYPE        default RelWithDebInfo. The installed Qt fixes the set of
rem                     configurations this build tree may use (QtBuildInternalsExtra
rem                     force-sets CMAKE_CONFIGURATION_TYPES to RelWithDebInfo;Debug),
rem                     so this must be one of them - "Release" is not, and would
rem                     build for hours and then fail at the install step. Of the two,
rem                     only RelWithDebInfo produces unsuffixed DLL names that can be
rem                     laid over a PySide6 wheel; Debug appends the debug postfix;
rem   PARALLEL          default empty = all cores; CI passes a RAM-aware value
rem   USE_CCACHE        default 1 = inject gn cc_wrapper="ccache"
rem   CCACHE_DIR        default %WORK_ROOT%\ccache   (ccache.conf is written here)
rem   CCACHE_MAX_SIZE   default 20G
rem   JUMBO             default 0 = pass -webengine-jumbo-build to configure
rem   RESET             1/true = delete source/build/install dirs first
rem   SHALLOW_SUBMODULE default 1/true = shallow Chromium submodule
rem   SKIP_PATCH        1/true = skip both patches
rem
rem Exit codes: 1 env/args, 2 source or submodule, 3 patch, 4 configure, 5 build,
rem             6 install, 7 package
rem ============================================================================

set "SCRIPT_DIR=%~dp0"
set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"

set "PHASE=%~1"
if not defined PHASE set "PHASE=all"

call :defaults

if /i "%PHASE%"=="prepare" goto :phase_prepare
if /i "%PHASE%"=="build" goto :phase_build
if /i "%PHASE%"=="finish" goto :phase_finish
if /i "%PHASE%"=="all" goto :phase_all
echo [error] unknown phase "%PHASE%"; use all, prepare, build or finish
exit /b 1

:phase_all
rem Stale state files from an earlier round must not be mistaken for this round's
rem outcome: clear it, then let prepare/build record what actually happened.
if exist "%STATE_FILE%" del "%STATE_FILE%"
call :step_prepare
if errorlevel 1 (
    rem Same reason as in :phase_prepare: without a state file the caller reads this
    rem deterministic failure as a time-out and queues another identical round.
    if not exist "%WORK_ROOT%" mkdir "%WORK_ROOT%" 2>nul
    > "%STATE_FILE%" echo failed 1
    echo [error] prepare failed; state file says failed
    exit /b 1
)
call :step_build
if errorlevel 1 exit /b %errorlevel%
call :step_finish
if errorlevel 1 exit /b %errorlevel%
goto :done

:phase_prepare
rem Start from a clean slate. The prepare phase is deterministic and re-runnable, so any
rem state file left by an earlier round is stale. Clearing it first means: killed while
rem preparing -> no file -> "killed", resume later; prepare fails -> we write "failed"
rem below -> the caller stops instead of queueing an identical round.
if exist "%STATE_FILE%" del "%STATE_FILE%"
call :step_prepare
set "RC=%errorlevel%"
if not "%RC%"=="0" (
    rem Record the failure so the caller does not read an absent state file as "the
    rem time budget ran out, try again". Without this the phases loop forever: every
    rem deterministic prepare error - a bad configure flag, a broken tool shim - left
    rem the file absent, the run reported "killed", and the next round failed the same
    rem way.
    if not exist "%WORK_ROOT%" mkdir "%WORK_ROOT%" 2>nul
    > "%STATE_FILE%" echo failed %RC%
    echo [error] prepare failed, code %RC%; state file says failed - no next round will be queued
    exit /b %RC%
)
goto :done

:phase_build
call :step_build
if errorlevel 1 exit /b %errorlevel%
goto :done

:phase_finish
call :step_finish
if errorlevel 1 exit /b %errorlevel%
goto :done

:done
echo.
echo [done] phase %PHASE%
echo [done] built runtime : %INSTALL_PREFIX%
echo [done] staging dir   : %DIST_DIR%
exit /b 0

rem ---------------------------------------------------------------------------
rem step_prepare: environment -> source -> patches -> configure
rem ---------------------------------------------------------------------------

:step_prepare
call :check_env
if errorlevel 1 exit /b 1
call :prepare_dirs
if errorlevel 1 exit /b 1
call :vs_env
if errorlevel 1 exit /b 1
call :check_tools
if errorlevel 1 exit /b 1
call :ccache_setup
if errorlevel 1 exit /b 1
call :fetch_source
if errorlevel 1 exit /b 2
call :patch
if errorlevel 1 exit /b 3
call :configure
if errorlevel 1 exit /b 4
exit /b 0

rem ---------------------------------------------------------------------------
rem step_build: build only, and record the outcome in STATE_FILE
rem ---------------------------------------------------------------------------

:step_build
rem Anything that stops this phase before cmake starts (bad environment, missing
rem tools, no configure) is a failure, not a time-out: record it up front, so the
rem caller does not read the absent state file as "resume later" and dispatch
rem another round that fails the same way.
mkdir "%WORK_ROOT%" 2>nul
> "%STATE_FILE%" echo failed 1
call :check_env
if errorlevel 1 exit /b 1
call :vs_env
if errorlevel 1 exit /b 1
call :check_tools
if errorlevel 1 exit /b 1
call :ccache_setup
if errorlevel 1 exit /b 1
if not exist "%BUILD_DIR%\CMakeCache.txt" (
    echo [error] no CMakeCache.txt in "%BUILD_DIR%"; run the prepare phase first
    exit /b 1
)
rem Cleared just before cmake runs: a build killed from outside leaves the file
rem absent, and an absent file is what the caller reads as "not finished, resume
rem later". Every path above has already recorded "failed".
if exist "%STATE_FILE%" del "%STATE_FILE%"
call :build
if errorlevel 1 exit /b 5
call :ccache_report
exit /b 0

rem ---------------------------------------------------------------------------
rem step_finish: install -> package
rem ---------------------------------------------------------------------------

:step_finish
call :check_env
if errorlevel 1 exit /b 1
call :vs_env
if errorlevel 1 exit /b 1
call :check_tools
if errorlevel 1 exit /b 1
call :install
if errorlevel 1 exit /b 6
call :package
if errorlevel 1 exit /b 7
exit /b 0

rem ---------------------------------------------------------------------------
rem helpers
rem ---------------------------------------------------------------------------

rem Run a sibling PowerShell script with whichever PowerShell is available.
rem pwsh is preferred; Windows PowerShell 5.1 is trimmed out of some images.
:run_ps
set "PS_EXE=powershell"
where pwsh >nul 2>nul
if not errorlevel 1 set "PS_EXE=pwsh"
%PS_EXE% -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%\%~1" %2 %3 %4 %5 %6
exit /b %errorlevel%

:defaults
if not defined WORK_ROOT set "WORK_ROOT=%SystemDrive%\qtwebengine-work"
if not defined SRC_DIR set "SRC_DIR=%WORK_ROOT%\src\qtwebengine"
if not defined BUILD_DIR set "BUILD_DIR=%WORK_ROOT%\build"
if not defined INSTALL_PREFIX set "INSTALL_PREFIX=%WORK_ROOT%\install"
if not defined DIST_DIR set "DIST_DIR=%WORK_ROOT%\dist"
if not defined TOOLS_BIN set "TOOLS_BIN=%WORK_ROOT%\tools\bin"
if not defined QT_TOOLS_DIR set "QT_TOOLS_DIR=%SystemDrive%\Qt\Tools"
if not defined STATE_FILE set "STATE_FILE=%WORK_ROOT%\build-state.txt"
if not defined QT_VERSION set "QT_VERSION=6.8.3"
if not defined QTWEBENGINE_REF set "QTWEBENGINE_REF=%QT_VERSION%"
if not defined QT_SOURCE_URL set "QT_SOURCE_URL=https://github.com/qt/qtwebengine.git"
if not defined BUILD_TYPE set "BUILD_TYPE=RelWithDebInfo"
if not defined CCACHE_DIR set "CCACHE_DIR=%WORK_ROOT%\ccache"
if not defined CCACHE_MAX_SIZE set "CCACHE_MAX_SIZE=20G"
rem Booleans accept both 1/0 and true/false; CI boolean inputs arrive as true/false
if /i "%USE_CCACHE%"=="true" set "USE_CCACHE=1"
if /i "%USE_CCACHE%"=="false" set "USE_CCACHE=0"
if not defined USE_CCACHE set "USE_CCACHE=1"
if /i "%JUMBO%"=="true" set "JUMBO=1"
if /i "%JUMBO%"=="false" set "JUMBO=0"
if not defined JUMBO set "JUMBO=0"
if /i "%SHALLOW_SUBMODULE%"=="true" set "SHALLOW_SUBMODULE=1"
if /i "%SHALLOW_SUBMODULE%"=="false" set "SHALLOW_SUBMODULE=0"
if not defined SHALLOW_SUBMODULE set "SHALLOW_SUBMODULE=1"
if /i "%SKIP_PATCH%"=="true" set "SKIP_PATCH=1"
if /i "%SKIP_PATCH%"=="false" set "SKIP_PATCH=0"
if /i "%RESET%"=="true" set "RESET=1"
if /i "%RESET%"=="false" set "RESET=0"
exit /b 0

:check_env
if not defined QT_PATH (
    echo [error] QT_PATH is not set; it must point at a Qt msvc2022_64 prefix
    exit /b 1
)
if not exist "%QT_PATH%\bin\qt-configure-module.bat" (
    echo [error] not found: "%QT_PATH%\bin\qt-configure-module.bat"
    echo [error] QT_PATH is wrong, or the Qt install is incomplete
    exit /b 1
)
rem A from-source module links Qt private APIs, so private headers are required.
rem Official binary installs carry them; if they are missing, qtbase has to be
rem built from source first. Better to say so here than to fail hours in.
set "QT_PRIV="
for /d %%d in ("%QT_PATH%\include\QtCore\6.*") do if exist "%%d\QtCore\private" set "QT_PRIV=%%d"
if not defined QT_PRIV (
    echo [error] Qt private headers not found under "%QT_PATH%\include\QtCore\"
    echo [error] QtWebEngine links Qt private APIs; make sure the msvc2022_64 package is complete
    exit /b 1
)
echo [env] Qt prefix      : %QT_PATH%
echo [env] build type     : %BUILD_TYPE%   parallel: %PARALLEL%
echo [env] ccache         : use=%USE_CCACHE% dir=%CCACHE_DIR%
echo [env] source / build : %SRC_DIR% / %BUILD_DIR%
exit /b 0

:prepare_dirs
if "%RESET%"=="1" (
    echo [step] RESET=1: removing old source / build / install dirs
    if exist "%SRC_DIR%" rmdir /s /q "%SRC_DIR%"
    if exist "%BUILD_DIR%" rmdir /s /q "%BUILD_DIR%"
    if exist "%INSTALL_PREFIX%" rmdir /s /q "%INSTALL_PREFIX%"
)
mkdir "%WORK_ROOT%" 2>nul
mkdir "%WORK_ROOT%\src" 2>nul
mkdir "%TOOLS_BIN%" 2>nul
mkdir "%DIST_DIR%" 2>nul
exit /b 0

:vs_env
if defined VCINSTALLDIR (
    echo [env] MSVC environment already initialized
    exit /b 0
)
set "PF86=%ProgramFiles(x86)%"
if not exist "%PF86%\Microsoft Visual Studio\Installer\vswhere.exe" (
    echo [error] vswhere.exe not found; install Visual Studio 2022 with the
    echo [error] "Desktop development with C++" workload
    exit /b 1
)
set "VSINSTALL="
for /f "usebackq delims=" %%i in (`"%PF86%\Microsoft Visual Studio\Installer\vswhere.exe" -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath`) do set "VSINSTALL=%%i"
if not defined VSINSTALL (
    echo [error] vswhere found no Visual Studio with the C++ toolset
    exit /b 1
)
if not exist "%VSINSTALL%\VC\Auxiliary\Build\vcvars64.bat" (
    echo [error] not found: "%VSINSTALL%\VC\Auxiliary\Build\vcvars64.bat"
    exit /b 1
)
echo [env] Visual Studio  : %VSINSTALL%
call "%VSINSTALL%\VC\Auxiliary\Build\vcvars64.bat" >nul
if errorlevel 1 (
    echo [error] vcvars64.bat failed
    exit /b 1
)
exit /b 0

:check_tools
rem Chromium's gn and CMake look for tools named bison/flex, while winflexbison installs
rem win_bison/win_flex. Bring those two names into TOOLS_BIN rather than putting the whole
rem winflexbison dir on PATH, where it would collide with MSYS2's bison, which the Qt docs
rem explicitly warn against using.
rem Do not copy win_bison.exe itself when it comes from chocolatey: that file is a shim
rem resolving its target through a path relative to its own location, so a copy dies with
rem "Cannot find file at '..\lib\winflexbison3\tools\win_flex.exe'" - measured on the
rem runner. :shim_bison_flex takes the real tools directory instead; it also carries
rem bison's data/ files, which the real executables look up next to themselves.
call :shim_bison_flex
rem Qt's bin goes first: qt-configure-module must match the Qt DLLs it loads, and
rem Qt's own Ninja is preferred over whatever the image ships. TOOLS_BIN follows,
rem so the shims - and ccache, when the workflow drops it there - beat any
rem preinstalled copy.
set "PATH=%QT_PATH%\bin;%QT_PATH%\Src\qtbase\bin;%QT_TOOLS_DIR%\Ninja;%TOOLS_BIN%;%PATH%"
set "MISSINGFILE=%TEMP%\qtwebengine-missing-tools.txt"
if exist "%MISSINGFILE%" del "%MISSINGFILE%"
for %%t in (cl cmake ninja node perl python gperf bison flex git) do where %%t >nul 2>nul || echo %%t>>"%MISSINGFILE%"
if exist "%MISSINGFILE%" (
    echo [error] missing build dependencies:
    type "%MISSINGFILE%"
    del "%MISSINGFILE%"
    echo [error] QtWebEngine needs a C++20 compiler, CMake, Ninja, Node 20+,
    echo [error] Python3 with html5lib and spdx-tools, gperf, bison, flex, Perl
    exit /b 1
)
echo [env] cl / cmake / ninja / node / perl / python / gperf / bison / flex all on PATH
rem Presence is not enough. CMake's FindBISON/FindFLEX run "<tool> --version" and hard
rem fail if it does not answer, and a chocolatey shim copy passes "where" but dies when
rem executed - that is exactly how a round was lost: bison.exe was on PATH, the check
rem said fine, and configure died minutes later. Run them here instead; bison must also
rem find its data/ directory next to the executable, so use a real invocation.
bison --version >nul 2>&1
if errorlevel 1 (
    echo [error] bison is on PATH but does not run: FindBISON will fail
    echo [error] if it came from chocolatey, the shim was copied instead of the real tool
    echo [error] run: where bison
    exit /b 1
)
flex --version >nul 2>&1
if errorlevel 1 (
    echo [error] flex is on PATH but does not run: FindFLEX will fail
    echo [error] run: where flex
    exit /b 1
)
exit /b 0

rem Put working bison/flex names into TOOLS_BIN. winflexbison's real tools directory is
rem copied whole so the executables keep their data/ siblings, then the plain names are
rem added as further copies. Called on every run so a stale or half-copied TOOLS_BIN heals.
:shim_bison_flex
set "WF_DIR="
for /f "delims=" %%i in ('where win_bison 2^>nul') do if not defined WF_DIR set "WF_DIR=%%~dpi"
if not defined WF_DIR exit /b 0
rem A chocolatey shim sits in <choco>\bin and resolves to <choco>\lib\winflexbison3\tools
set "WF_REAL="
for %%i in ("%WF_DIR%..\lib\winflexbison3\tools") do if exist "%%~fi\win_bison.exe" set "WF_REAL=%%~fi"
if defined WF_REAL (
    echo [env] bison / flex   : "%WF_REAL%" - chocolatey shim resolved to the real tools
    xcopy "%WF_REAL%\*" "%TOOLS_BIN%\" /e /i /q /y >nul
    copy /y "%TOOLS_BIN%\win_bison.exe" "%TOOLS_BIN%\bison.exe" >nul
    if exist "%TOOLS_BIN%\win_flex.exe" copy /y "%TOOLS_BIN%\win_flex.exe" "%TOOLS_BIN%\flex.exe" >nul
) else (
    echo [env] bison / flex   : "%WF_DIR%" - not a chocolatey shim, copied as-is
    rem Bring bison's data/ along when it sits next to the executable: --version works
    rem without it, but real parsing dies with "data/m4sugar/m4sugar.m4: cannot open".
    if exist "%WF_DIR%data" xcopy "%WF_DIR%data" "%TOOLS_BIN%\data\" /e /i /q /y >nul
    if not exist "%TOOLS_BIN%\bison.exe" copy /y "%WF_DIR%win_bison.exe" "%TOOLS_BIN%\bison.exe" >nul
    if not exist "%TOOLS_BIN%\flex.exe" if exist "%WF_DIR%win_flex.exe" copy /y "%WF_DIR%win_flex.exe" "%TOOLS_BIN%\flex.exe" >nul
)
exit /b 0

:ccache_setup
if not "%USE_CCACHE%"=="1" (
    echo [step] USE_CCACHE=0: building without a compiler cache
    exit /b 0
)
where ccache >nul 2>nul
if errorlevel 1 (
    echo [error] ccache.exe not on PATH; drop it in "%TOOLS_BIN%" or set USE_CCACHE=0
    exit /b 1
)
mkdir "%CCACHE_DIR%" 2>nul
rem ccache reads ccache.conf from CCACHE_DIR. base_dir makes source paths
rem relative, so a cache filled under one run's directory still hits under the
rem next run's directory - CI hands us a fresh workspace every round.
rem compiler_check=none is what the kiwi Chromium build uses; this cache is
rem private, so hashing the compiler on every call is not worth the lower hit
rem rate. sloppiness admits that Chromium's build embeds time macros and mtimes.
rem stats is left at its default (on) on purpose: the workflow reads the counters
rem to prove the injected cc_wrapper bound - "stats = false" would zero them and
rem fail the round even when ccache works, and would also switch off automatic
rem cleanup, so max_size would stop being enforced.
> "%CCACHE_DIR%\ccache.conf" (
    echo compiler_check = none
    echo max_size = %CCACHE_MAX_SIZE%
    echo base_dir = %WORK_ROOT%
    echo cache_dir = %CCACHE_DIR%
    echo hash_dir = false
    echo sloppiness = include_file_ctime,include_file_mtime,time_macros,locale
)
for /f "delims=" %%v in ('ccache --version ^| findstr /r /c:"^ccache version"') do echo [env] %%v
exit /b 0

:ccache_report
if not "%USE_CCACHE%"=="1" exit /b 0
echo [step] ccache statistics after this build:
call ccache --show-stats
rem A Cacheable calls counter above zero is the proof that the injected
rem cc_wrapper gn arg actually bound - see patch-gn-args.ps1. Zero (or a counter
rem that cannot be read) means the next round gains nothing, which is worth
rem shouting about rather than burning hours on. This is only a warning here:
rem the build itself succeeded, so it must not turn the phase into a failure.
set "CACHEABLE="
for /f "tokens=3" %%a in ('ccache --show-stats ^| findstr /i /c:"Cacheable calls"') do set "CACHEABLE=%%a"
if defined CACHEABLE (
    if "%CACHEABLE%"=="0" echo [warn] ccache was never called: cc_wrapper did not bind, the next round gains nothing
) else (
    echo [warn] could not read the Cacheable calls counter from ccache --show-stats
)
exit /b 0

:fetch_source
if exist "%SRC_DIR%" if not exist "%SRC_DIR%\.git" (
    echo [error] "%SRC_DIR%" exists but is not a git repo; set RESET=1 or empty it
    exit /b 1
)
if exist "%SRC_DIR%\.git" (
    echo [step] source already present, skipping clone: %SRC_DIR%
) else (
    echo [step] cloning qtwebengine %QTWEBENGINE_REF% from %QT_SOURCE_URL%
    git clone --branch "%QTWEBENGINE_REF%" --depth 1 "%QT_SOURCE_URL%" "%SRC_DIR%"
    if errorlevel 1 (
        echo [error] clone failed
        exit /b 1
    )
)
call :submodule_update
if errorlevel 1 exit /b 1
exit /b 0

:submodule_update
rem All of Chromium lives in the single src/3rdparty submodule - by far the
rem biggest part of this build (tens of GB checked out).
if exist "%SRC_DIR%\src\3rdparty\chromium\v8" (
    echo [step] Chromium submodule already present, skipping
    exit /b 0
)
if not "%SHALLOW_SUBMODULE%"=="1" goto :submodule_full
echo [step] fetching Chromium submodule - shallow
git -C "%SRC_DIR%" submodule update --init --depth 1 -- src/3rdparty
if not errorlevel 1 exit /b 0
echo [warn] shallow submodule fetch failed, falling back to a full fetch
:submodule_full
echo [step] fetching Chromium submodule - full
git -C "%SRC_DIR%" submodule update --init -- src/3rdparty
if errorlevel 1 (
    echo [error] submodule fetch failed
    exit /b 1
)
exit /b 0

:patch
if "%SKIP_PATCH%"=="1" (
    echo [step] SKIP_PATCH=1: skipping patches
    exit /b 0
)
echo [step] patch 1/3: Chromium cppgc - MSVC 14.44 reports C2352 on Qt 6.8.3 V8
call :run_ps patch-cppgc.ps1 -SourceRoot "%SRC_DIR%"
if errorlevel 1 (
    echo [error] cppgc patch failed
    exit /b 1
)
echo [step] patch 2/3: single configuration - the VS generator would build RelWithDebInfo and Debug
call :run_ps patch-single-config.ps1 -SourceRoot "%SRC_DIR%"
if errorlevel 1 (
    echo [error] single-config patch failed
    exit /b 1
)
if "%USE_CCACHE%"=="1" (
    echo [step] patch 3/3: gn cc_wrapper=ccache into src/core/CMakeLists.txt
    call :run_ps patch-gn-args.ps1 -SourceRoot "%SRC_DIR%" -UseCcache
) else (
    echo [step] patch 3/3: clearing previously injected gn args
    call :run_ps patch-gn-args.ps1 -SourceRoot "%SRC_DIR%"
)
if errorlevel 1 (
    echo [error] gn args patch failed
    exit /b 1
)
exit /b 0

:configure
if not exist "%BUILD_DIR%\CMakeCache.txt" goto :configure_run
echo [step] CMakeCache.txt present, skipping configure; set RESET=1 to reconfigure
rem Still validate: a cache left by an earlier round may have been configured with a
rem BUILD_TYPE this tree cannot build and install. Checking here costs nothing and
rem turns a multi-hour build-then-fail into an immediate error.
call :assert_config
if errorlevel 1 exit /b 1
exit /b 0

:configure_run
mkdir "%BUILD_DIR%" 2>nul
set "JUMBO_FLAG="
if "%JUMBO%"=="1" set "JUMBO_FLAG=-webengine-jumbo-build"
pushd "%BUILD_DIR%"
echo [step] qt-configure-module -webengine-proprietary-codecs; it downloads the Chromium toolchain
rem No -nomake examples/-nomake tests here: those belong to the top-level Qt configure
rem script. qt-configure-module takes only -DFEATURE_* and, after "--", CMake arguments -
rem passing -nomake fails with "Unknown command line option '-nomake'" (measured on
rem Qt 6.8.3). A single-module build adds neither examples nor tests anyway, so there
rem is nothing to turn off.
rem -DCMAKE_BUILD_TYPE must be a configuration the installed Qt actually offers.
rem This is a module build, so Qt's configure wrapper passes no -G and CMake picks its
rem Windows default, "Visual Studio 17 2022" - a multi-config generator. Such a
rem generator ignores CMAKE_BUILD_TYPE when it compiles, and the installed Qt force-sets
rem CMAKE_CONFIGURATION_TYPES to "RelWithDebInfo;Debug" (measured: "Building for multiple
rem configurations: RelWithDebInfo;Debug."). So the value here does not choose what gets
rem compiled - --config does that in :build and :install - but it is read by Qt's
rem get_install_config(), which prefers it over the configuration list. Naming a
rem configuration that is not in that list (the old default, Release) makes the gn
rem install rule target a configuration that does not exist. :assert_config below
rem checks both facts before anything expensive starts.
call "%QT_PATH%\bin\qt-configure-module.bat" "%SRC_DIR%" -webengine-proprietary-codecs %JUMBO_FLAG% -- -DQT_SHOW_EXTRA_IDE_SOURCES=OFF -DCMAKE_INSTALL_PREFIX="%INSTALL_PREFIX%" -DCMAKE_BUILD_TYPE=%BUILD_TYPE% -DQTWE_BUILD_CONFIGURATION=%BUILD_TYPE%
set "RC=%errorlevel%"
popd
if not "%RC%"=="0" (
    echo [error] configure failed, code %RC%
    exit /b 1
)
if not exist "%BUILD_DIR%\CMakeCache.txt" (
    echo [error] configure reported success but produced no CMakeCache.txt
    exit /b 1
)
rem Fail in seconds rather than hours: confirm the configuration we will build and
rem install is one this tree actually offers. --build and --install both take
rem --config %BUILD_TYPE%, and a bare "cmake --build ." would silently pick Debug.
call :assert_config
if errorlevel 1 exit /b 1
exit /b 0

:assert_config
rem Two shapes are possible: a multi-config tree lists CMAKE_CONFIGURATION_TYPES, a
rem single-config tree has CMAKE_BUILD_TYPE. Compare at top level only - inside a
rem parenthesised block %VAR% is expanded when the block is parsed, so a value set in
rem the same block cannot be read back there (this script does not enable delayed
rem expansion).
rem
rem CMAKE_BUILD_TYPE is checked as well, even in a multi-config tree where it does not
rem select what compiles: Qt's get_install_config() reads it first, so a value that
rem disagrees with BUILD_TYPE silently gives an install rule for the wrong
rem configuration. That mismatch is what an earlier round shipped.
set "BT_LINE="
for /f "usebackq tokens=1,* delims==" %%a in (`findstr /b /c:"CMAKE_BUILD_TYPE:" "%BUILD_DIR%\CMakeCache.txt"`) do set "BT_LINE=%%b"
if not defined BT_LINE goto :assert_config_skip_bt
if /i not "%BT_LINE%"=="%BUILD_TYPE%" goto :assert_config_bt_mismatch

:assert_config_skip_bt
findstr /b /c:"CMAKE_CONFIGURATION_TYPES:" "%BUILD_DIR%\CMakeCache.txt" >nul
if errorlevel 1 goto :assert_config_single
set "CFG_LINE="
for /f "usebackq tokens=1,* delims==" %%a in (`findstr /b /c:"CMAKE_CONFIGURATION_TYPES:" "%BUILD_DIR%\CMakeCache.txt"`) do set "CFG_LINE=%%b"
if not defined CFG_LINE goto :assert_config_unknown
echo [env] configuration set: %CFG_LINE%
echo %CFG_LINE% | findstr /i /c:"%BUILD_TYPE%" >nul
if errorlevel 1 goto :assert_config_bad
rem It must also be the ONLY configuration. The Visual Studio generator builds every
rem configuration in this list, and QtWebEngine wires one gn/ninja tree per entry with
rem each one a dependency of WebEngineCore - so two entries compile the whole of
rem Chromium twice (measured: the first tree reached 8038/29705 in 56 minutes with the
rem second queued behind it). That runtime is never used: only the requested
rem configuration is installed. patch-single-config.ps1 narrows the list; if it did not
rem take effect, fail here in seconds instead of after hours.
if /i not "%CFG_LINE%"=="%BUILD_TYPE%" goto :assert_config_multi
rem Debug output carries CMAKE_DEBUG_POSTFIX, so its DLLs cannot be laid over PySide6.
if /i "%BUILD_TYPE%"=="Debug" echo [warn] BUILD_TYPE=Debug: DLLs get the debug postfix and cannot overlay PySide6
echo [env] configuration  : %BUILD_TYPE% - verified present and single
exit /b 0

:assert_config_single
if /i not "%BT_LINE%"=="%BUILD_TYPE%" goto :assert_config_unknown
echo [env] configuration  : %BUILD_TYPE% - single-config generator
exit /b 0

:assert_config_bad
echo [error] BUILD_TYPE=%BUILD_TYPE% is not one of the configurations this tree offers
echo [error] the installed Qt decides that set; pick one of the values printed above,
echo [error] or delete "%BUILD_DIR%" and configure again
exit /b 1

:assert_config_bt_mismatch
echo [error] CMakeCache.txt has CMAKE_BUILD_TYPE=%BT_LINE% but this run builds %BUILD_TYPE%
echo [error] Qt's get_install_config prefers CMAKE_BUILD_TYPE, so the install rules
echo [error] would be generated for %BT_LINE% and the output would not be installed
echo [error] delete "%BUILD_DIR%" and run the prepare phase again
exit /b 1

:assert_config_multi
echo [error] CMAKE_CONFIGURATION_TYPES is "%CFG_LINE%" but must be exactly "%BUILD_TYPE%"
echo [error] the Visual Studio generator builds every configuration in that list, and
echo [error] QtWebEngine wires one gn/ninja tree per entry, each a dependency of
echo [error] WebEngineCore, so two entries compile the whole of Chromium twice
echo [error] patch-single-config.ps1 must inject a CMAKE_CONFIGURATION_TYPES FORCE set
echo [error] after the Qt6 find_package in "%SRC_DIR%\CMakeLists.txt", and configure
echo [error] must receive -DQTWE_BUILD_CONFIGURATION=%BUILD_TYPE%
echo [error] delete "%BUILD_DIR%" and run the prepare phase again
exit /b 1

:assert_config_unknown
echo [error] cannot determine the configuration of "%BUILD_DIR%\CMakeCache.txt"
echo [error] CMAKE_CONFIGURATION_TYPES is absent and CMAKE_BUILD_TYPE is not %BUILD_TYPE%
exit /b 1

:build
pushd "%BUILD_DIR%"
rem --config is mandatory here. This tree uses the Visual Studio generator, and for a
rem multi-config generator a bare "cmake --build ." builds Debug (cmVS10Gen.cxx: with an
rem empty config it substitutes "Debug"). Debug output carries the debug postfix, so the
rem install step below, which asks for %BUILD_TYPE%, would find nothing to install.
if defined PARALLEL (
    echo [step] cmake --build . --config %BUILD_TYPE% --parallel %PARALLEL%
    call cmake --build . --config %BUILD_TYPE% --parallel %PARALLEL%
) else (
    echo [step] cmake --build . --config %BUILD_TYPE% --parallel
    call cmake --build . --config %BUILD_TYPE% --parallel
)
set "RC=%errorlevel%"
popd
rem 0xC000013A (STATUS_CONTROL_C_EXIT, arrives here as -1073741510) is what cmake
rem returns when the step's time budget cancels the process tree. Measured: the round
rem that compiled to [8038/29705] and was cut off at 60 minutes recorded
rem "failed -1073741510" and the caller therefore refused to queue a next round, even
rem though nothing was wrong with the build. An interruption is not a compile error:
rem leave STATE_FILE absent, which is what the caller reads as "killed, resume later".
rem A genuine compile error arrives here as ninja's small positive exit code via cmake,
rem never as this one, so this cannot mask a real failure.
if "%RC%"=="-1073741510" (
    echo [step] interrupted by the step time budget; state file left absent so the next round resumes
    exit /b 5
)
if "%RC%"=="0" (
    > "%STATE_FILE%" echo ok
    echo [step] build completed; state file says ok
    exit /b 0
)
> "%STATE_FILE%" echo failed %RC%
echo [error] build failed, code %RC%
exit /b 1

:install
pushd "%BUILD_DIR%"
echo [step] cmake --install . --config %BUILD_TYPE%
call cmake --install . --config %BUILD_TYPE%
set "RC=%errorlevel%"
popd
if not "%RC%"=="0" (
    echo [error] install failed, code %RC%
    exit /b 1
)
if not exist "%INSTALL_PREFIX%\bin\Qt6WebEngineCore.dll" (
    echo [error] no Qt6WebEngineCore.dll under "%INSTALL_PREFIX%\bin" after install
    echo [error] check whether CMake accepted INSTALL_PREFIX
    exit /b 1
)
for %%f in ("%INSTALL_PREFIX%\bin\Qt6WebEngineCore.dll") do echo [done] Qt6WebEngineCore.dll %%~zf bytes
exit /b 0

:package
echo [step] staging built runtime into %DIST_DIR%\qtwebengine-%QT_VERSION%-win64-msvc2022
call :run_ps install-webengine-runtime.ps1 -Source "%INSTALL_PREFIX%" -Destination "%DIST_DIR%\qtwebengine-%QT_VERSION%-win64-msvc2022" -Create
if errorlevel 1 (
    echo [error] packaging failed
    exit /b 1
)
exit /b 0
