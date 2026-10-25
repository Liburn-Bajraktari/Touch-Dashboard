package dev.touchdashboard.app;

import android.app.Activity;
import android.hardware.display.DisplayManager;
import android.os.Build;
import android.os.Handler;
import android.os.HandlerThread;
import android.os.Looper;
import android.os.PerformanceHintManager;
import android.os.Process;
import android.os.WorkDuration;
import android.view.Display;
import android.view.FrameMetrics;
import android.view.Window;

import java.io.File;
import java.io.FileInputStream;
import java.io.IOException;
import java.nio.charset.StandardCharsets;

/**
 * Tells the power HAL how long each frame took against the display's frame
 * time (ADPF, Android 12+), so it raises CPU clocks only while frames run late.
 *
 * WebView draws on threads of this process: HWUI's RenderThread runs WebView's
 * draw functor, which waits on VizWebView (the display compositor) and
 * Chrome_InProcGpuThread. Android gave the app no hint session of its own
 * (dumpsys performance_hint), and on a Redmi Pad 2 the big cores averaged
 * 1.27 of 2.2 GHz with the compositor on the little cores: a frame took about
 * 14 ms of the 11.1 ms a 90 Hz panel allows, and half of them were shown twice.
 * The page's renderer runs in WebView's sandboxed process (another uid) and
 * cannot be in this session.
 */
final class FrameHints implements Window.OnFrameMetricsAvailableListener, DisplayManager.DisplayListener {

    /** Thread names as the kernel keeps them (15 characters at most). */
    private static final String[] THREADS = {"RenderThread", "VizWebView", "Chrome_InProcGp"};
    /** WebView starts its threads with its first frame: look again until they are there. */
    private static final long RETRY_MS = 1000;
    private static final int MAX_TRIES = 30;

    private final Activity activity;
    private final Handler main = new Handler(Looper.getMainLooper());
    private HandlerThread metricsThread;
    private Handler metrics;
    /** Created on the main thread; reported to and closed on the metrics thread. */
    private volatile PerformanceHintManager.Session session;
    private boolean running;
    private int tries;

    FrameHints(Activity activity) {
        this.activity = activity;
    }

    /** onResume: open a session once WebView's threads exist. */
    void start() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.S || running) return;
        running = true;
        tries = 0;
        main.postDelayed(this::open, RETRY_MS);
    }

    /** onPause: in the background nothing is drawn, and the HAL should not boost for it. */
    void stop() {
        if (!running) return;
        running = false;
        main.removeCallbacksAndMessages(null);
        if (metricsThread == null) return;
        activity.getWindow().removeOnFrameMetricsAvailableListener(this);
        DisplayManager displays = activity.getSystemService(DisplayManager.class);
        if (displays != null) displays.unregisterDisplayListener(this);
        // Closed after any report already queued on that thread.
        metrics.post(() -> {
            PerformanceHintManager.Session s = session;
            session = null;
            if (s != null) s.close();
        });
        metricsThread.quitSafely();
        metricsThread = null;
        metrics = null;
    }

    private void open() {
        if (!running) return;
        int[] tids = renderingThreads();
        if (tids == null) {
            if (++tries < MAX_TRIES) main.postDelayed(this::open, RETRY_MS);
            return;
        }
        PerformanceHintManager hints = activity.getSystemService(PerformanceHintManager.class);
        if (hints == null) return;
        PerformanceHintManager.Session s;
        try {
            s = hints.createHintSession(tids, frameNanos());
        } catch (SecurityException | IllegalArgumentException e) {
            return;   // a thread went away meanwhile, or the system refuses: draw without hints
        }
        if (s == null) return;   // no ADPF on this device
        session = s;
        metricsThread = new HandlerThread("FrameHints");
        metricsThread.start();
        metrics = new Handler(metricsThread.getLooper());
        activity.getWindow().addOnFrameMetricsAvailableListener(this, metrics);
        DisplayManager displays = activity.getSystemService(DisplayManager.class);
        if (displays != null) displays.registerDisplayListener(this, metrics);
    }

    /** This (UI) thread and WebView's rendering threads, or null until they all exist. */
    private static int[] renderingThreads() {
        int[] tids = new int[THREADS.length + 1];
        tids[0] = Process.myPid();   // the main thread's id is the process id
        File[] tasks = new File("/proc/self/task").listFiles();
        if (tasks == null) return null;
        for (int i = 0; i < THREADS.length; i++) {
            for (File task : tasks) {
                if (THREADS[i].equals(readComm(task))) {
                    tids[i + 1] = Integer.parseInt(task.getName());
                    break;
                }
            }
            if (tids[i + 1] == 0) return null;
        }
        return tids;
    }

    private static String readComm(File task) {
        try (FileInputStream in = new FileInputStream(new File(task, "comm"))) {
            byte[] buf = new byte[32];
            int n = in.read(buf);
            return n > 0 ? new String(buf, 0, n, StandardCharsets.US_ASCII).trim() : "";
        } catch (IOException e) {
            return "";   // the thread ended
        }
    }

    private long frameNanos() {
        Display display = activity.getWindow().getDecorView().getDisplay();
        float hz = display != null ? display.getRefreshRate() : 60f;
        return (long) (1_000_000_000L / Math.max(hz, 1f));
    }

    /**
     * The frame's work on the UI thread and the RenderThread (which includes
     * waiting for WebView's compositor), not the wait for the next vsync before
     * it started: that wait is not work and would only keep the clocks up.
     * Android 15+ also takes the frame's GPU time and its whole span, so the
     * HAL can raise the GPU's clock too: reporting CPU work alone, frames that
     * waited on the GPU looked on time, and the Redmi Pad 2's GPU sat at
     * ~570 MHz of its 1 GHz while the marquee ran at 57 fps.
     */
    @Override
    public void onFrameMetricsAvailable(Window window, FrameMetrics m, int dropped) {
        PerformanceHintManager.Session s = session;
        if (s == null || m.getMetric(FrameMetrics.FIRST_DRAW_FRAME) == 1) return;
        long work = m.getMetric(FrameMetrics.INPUT_HANDLING_DURATION)
                + m.getMetric(FrameMetrics.ANIMATION_DURATION)
                + m.getMetric(FrameMetrics.LAYOUT_MEASURE_DURATION)
                + m.getMetric(FrameMetrics.DRAW_DURATION)
                + m.getMetric(FrameMetrics.SYNC_DURATION)
                + m.getMetric(FrameMetrics.COMMAND_ISSUE_DURATION)
                + m.getMetric(FrameMetrics.SWAP_BUFFERS_DURATION);
        if (work <= 0) return;
        if (Build.VERSION.SDK_INT >= 35) {
            long gpu = m.getMetric(FrameMetrics.GPU_DURATION);
            long total = Math.max(work, m.getMetric(FrameMetrics.TOTAL_DURATION));
            try {
                WorkDuration w = new WorkDuration();
                w.setWorkPeriodStartTimestampNanos(m.getMetric(FrameMetrics.INTENDED_VSYNC_TIMESTAMP));
                w.setActualTotalDurationNanos(total);
                w.setActualCpuDurationNanos(work);
                w.setActualGpuDurationNanos(Math.max(0, gpu));
                s.reportActualWorkDuration(w);
                return;
            } catch (IllegalArgumentException e) {
                // A metric this device does not fill: report the CPU work alone.
            }
        }
        s.reportActualWorkDuration(work);
    }

    /** The panel switches between 90, 60 and 50 Hz: keep the target at its frame time. */
    @Override
    public void onDisplayChanged(int displayId) {
        PerformanceHintManager.Session s = session;
        if (s != null) s.updateTargetWorkDuration(frameNanos());
    }

    @Override
    public void onDisplayAdded(int displayId) {
    }

    @Override
    public void onDisplayRemoved(int displayId) {
    }
}
