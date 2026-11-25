#[derive(Clone, Copy, Default)]
pub struct Rect {
    pub left: f64,
    pub top: f64,
    pub right: f64,
    pub bottom: f64,
}

impl Rect {
    pub fn centered(x: f64, y: f64, width: f64, height: f64) -> Self {
        Self {
            left: x - width / 2.,
            top: y - height / 2.,
            right: x + width / 2.,
            bottom: y + height / 2.,
        }
    }
    pub fn expand(self, dx: f64, dy: f64) -> Self {
        Self {
            left: self.left - dx,
            top: self.top - dy,
            right: self.right + dx,
            bottom: self.bottom + dy,
        }
    }
    pub fn intersects(self, b: Self) -> bool {
        self.left < b.right && b.left < self.right && self.top < b.bottom && b.top < self.bottom
    }
    pub fn distance_sq(self, x: f64, y: f64) -> f64 {
        let dx = (self.left - x).max(x - self.right).max(0.);
        let dy = (self.top - y).max(y - self.bottom).max(0.);
        dx * dx + dy * dy
    }
}

#[derive(Clone, Copy, Default)]
pub struct Pose {
    pub x: f64,
    pub y: f64,
    pub w: f64,
    pub h: f64,
    pub body: Rect,
    pub gap_box: Rect,
    pub courtyard: Rect,
    pub label_neighborhood: Rect,
}

impl Pose {
    pub fn new(x: f64, y: f64, w: f64, h: f64, gap: f64, text: Option<[f64; 2]>) -> Self {
        let body = Rect::centered(x, y, w, h);
        let courtyard = body.expand(0.2, 0.2);
        let [tw, th] = text.unwrap_or_default();
        let dx = tw / 2. + 3.;
        let dy = th / 2. + 3.;
        let reach = dx.hypot(dy);
        let label_neighborhood =
            courtyard.expand((dx + reach).max(tw + 3.), (dy + reach).max(th + 3.));
        Self {
            x,
            y,
            w,
            h,
            body,
            gap_box: Rect::centered(x, y, w + gap, h + gap),
            courtyard,
            label_neighborhood,
        }
    }
    pub fn distance(self, b: Self) -> f64 {
        (self.x - b.x).hypot(self.y - b.y)
    }
}

pub fn label_slots(
    pose: Pose,
    [w, h]: [f64; 2],
    [bw, bh]: [f64; 2],
    obstacles: &[(Rect, bool)],
) -> usize {
    let own = pose.courtyard;
    let cx = (own.left + own.right) / 2.;
    let cy = (own.top + own.bottom) / 2.;
    let mut first = None;
    for ring in 0..=6 {
        let d = f64::from(ring) * 0.5;
        for (ix, x) in [own.left - w / 2. - d, cx, own.right + w / 2. + d]
            .into_iter()
            .enumerate()
        {
            for (iy, y) in [own.top - h / 2. - d, cy, own.bottom + h / 2. + d]
                .into_iter()
                .enumerate()
            {
                if ix == 1 && iy == 1 {
                    continue;
                }
                let label = Rect::centered(x, y, w, h);
                if label.left < 0.4
                    || label.right > bw - 0.4
                    || label.top < 0.4
                    || label.bottom > bh - 0.4
                {
                    continue;
                }
                let dist = own.distance_sq(x, y);
                if obstacles.iter().any(|&(rect, peer)| {
                    label.intersects(rect) || peer && rect.distance_sq(x, y) < dist
                }) {
                    continue;
                }
                if let Some(point) = first {
                    if point != (x, y) {
                        return 2;
                    }
                } else {
                    first = Some((x, y));
                }
            }
        }
    }
    usize::from(first.is_some())
}
