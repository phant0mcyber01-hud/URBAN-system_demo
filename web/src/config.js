// Visual constants shared by the scene and the UI. The basemap is deliberately grey and quiet:
// on an operator's screen colour belongs to what is not normal (jams, incidents, devices in trouble).
export const BG = [14, 16, 19];
export const GROUND = [26, 29, 33];
export const FADE = 0.22; // share of the scene radius used for the fade-out at the rim
export const MAX_PIXEL_RATIO = 1.5; // cap HiDPI rendering: 2x+ quadruples fill cost for little gain

export const ROAD = {
  trunk: [96, 102, 110], primary: [96, 102, 110], secondary: [82, 88, 96], tertiary: [70, 76, 84],
};
export const ROAD_DEFAULT = [56, 61, 68];
export const MAJOR = new Set(['trunk', 'primary', 'secondary', 'tertiary']);
export const ROAD_RU = {
  trunk: 'магистраль', primary: 'главная улица', secondary: 'районная улица', tertiary: 'местная улица',
  residential: 'жилая улица', living_street: 'жилая зона', unclassified: 'проезд',
};

export const BUILDING = {
  apartments: [62, 66, 72], residential: [60, 64, 70], house: [64, 62, 60], detached: [64, 62, 60],
  school: [66, 64, 60], university: [66, 64, 60], kindergarten: [66, 64, 60], hospital: [64, 67, 70],
  retail: [63, 62, 67], commercial: [61, 65, 71], industrial: [56, 58, 61], mosque: [62, 67, 66],
};
export const BUILDING_DEFAULT = [59, 63, 68];
export const BUILDING_RU = {
  apartments: 'Жилой дом', residential: 'Жилой дом', house: 'Частный дом', detached: 'Частный дом',
  school: 'Школа', university: 'Университет', kindergarten: 'Детский сад', hospital: 'Больница',
  retail: 'Магазин', commercial: 'Коммерческое здание', industrial: 'Промышленное здание', mosque: 'Мечеть',
  garage: 'Гараж', garages: 'Гаражи', office: 'Офисное здание', hotel: 'Гостиница', public: 'Общественное здание',
};

export const AREA = { green: [31, 40, 34], water: [28, 39, 50], pitch: [33, 43, 36], parking: [31, 34, 38] };
export const MINOR = {
  service: { width: 3.4, color: [44, 48, 54] }, path: { width: 1.6, color: [54, 53, 50] },
  water: { width: 4, color: [36, 52, 68] }, rail: { width: 2, color: [74, 72, 72] },
};

export const POI = {
  hospital: { label: 'Больница', color: [214, 128, 138] },
  clinic: { label: 'Клиника', color: [214, 128, 138] },
  police: { label: 'Полиция', color: [128, 156, 214] },
  fire_station: { label: 'Пожарная часть', color: [214, 156, 104] },
};

export const STATUS = { ok: [120, 128, 138], warn: [227, 163, 58], alert: [229, 83, 75] };

// vehicle kinds by index: [length, width, height] in metres and colour
export const KINDS = ['car', 'bus', 'emg'];
export const VEHICLE_SIZE = [[4.4, 1.9, 1.5], [11.5, 2.6, 3.2], [6.2, 2.2, 2.7]];
export const VEHICLE_COLOR = [[178, 186, 196], [126, 158, 204], [255, 70, 64]];  // car, bus, emergency
export const EMG_BLINK = [[255, 70, 64], [90, 140, 255]];
