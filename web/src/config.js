// Visual constants shared by the scene and the UI (URBAN Grid, see style.css). The city is dark steel with its
// edges traced in the system's cyan; colour beyond that belongs to what is not normal (jams, incidents, devices).
export const ACCENT = [54, 214, 255];
export const BG = [4, 7, 10];
export const GROUND = [9, 14, 19];
export const FADE = 0.22; // share of the scene radius used for the fade-out at the rim
export const MAX_PIXEL_RATIO = 1.5; // cap HiDPI rendering: 2x+ quadruples fill cost for little gain

export const ROAD = {
  trunk: [58, 92, 108], primary: [58, 92, 108], secondary: [46, 74, 88], tertiary: [38, 60, 72],
};
export const ROAD_DEFAULT = [28, 44, 54];
export const MAJOR = new Set(['trunk', 'primary', 'secondary', 'tertiary']);
export const ROAD_RU = {
  trunk: 'магистраль', primary: 'главная улица', secondary: 'районная улица', tertiary: 'местная улица',
  residential: 'жилая улица', living_street: 'жилая зона', unclassified: 'проезд',
};

export const BUILDING = {
  apartments: [26, 36, 46], residential: [25, 34, 44], house: [24, 31, 39], detached: [24, 31, 39],
  school: [27, 35, 43], university: [27, 35, 43], kindergarten: [27, 35, 43], hospital: [28, 38, 47],
  retail: [26, 33, 42], commercial: [27, 37, 48], industrial: [22, 28, 35], mosque: [27, 37, 44],
};
export const BUILDING_DEFAULT = [24, 32, 41];
export const BUILDING_EDGE = [70, 170, 200];
export const BUILDING_RU = {
  apartments: 'Жилой дом', residential: 'Жилой дом', house: 'Частный дом', detached: 'Частный дом',
  school: 'Школа', university: 'Университет', kindergarten: 'Детский сад', hospital: 'Больница',
  retail: 'Магазин', commercial: 'Коммерческое здание', industrial: 'Промышленное здание', mosque: 'Мечеть',
  garage: 'Гараж', garages: 'Гаражи', office: 'Офисное здание', hotel: 'Гостиница', public: 'Общественное здание',
};

export const AREA = { green: [12, 26, 24], water: [8, 28, 44], pitch: [14, 28, 26], parking: [14, 20, 26] };
export const MINOR = {
  service: { width: 3.4, color: [22, 34, 42] }, path: { width: 1.6, color: [28, 40, 46] },
  water: { width: 4, color: [14, 46, 68] }, rail: { width: 2, color: [46, 58, 66] },
};

export const POI = {
  hospital: { label: 'Больница', color: [214, 128, 138] },
  clinic: { label: 'Клиника', color: [214, 128, 138] },
  police: { label: 'Полиция', color: [128, 156, 214] },
  fire_station: { label: 'Пожарная часть', color: [214, 156, 104] },
};

export const STATUS = { ok: [96, 128, 142], warn: [255, 181, 71], alert: [255, 84, 104] };

// vehicle kinds by index: [length, width, height] in metres and colour
export const KINDS = ['car', 'bus', 'emg'];
export const VEHICLE_SIZE = [[4.4, 1.9, 1.5], [11.5, 2.6, 3.2], [6.2, 2.2, 2.7]];
export const VEHICLE_COLOR = [[178, 186, 196], [126, 158, 204], [255, 70, 64]];  // car, bus, emergency
export const EMG_BLINK = [[255, 70, 64], [90, 140, 255]];
