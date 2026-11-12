// Bundle entry for the Forge-based dashboard.
// Only the components the dashboard actually uses are defined, to keep the
// bundle small. Add a define*Component call here when you introduce a new one.
import { IconRegistry } from '@tylertech/forge';
import { tylIconBrightness6, tylIconClipboard } from '@tylertech/tyler-icons';

// forge-icon renders NOTHING for an unregistered name -- it fails silently rather
// than erroring. The `external` attribute would fetch from the CDN instead, but that
// breaks the offline vendoring, so every icon used must be registered here.
IconRegistry.define([tylIconBrightness6, tylIconClipboard]);

import {
  defineAppBarComponent,
  defineBadgeComponent,
  defineButtonComponent,
  defineButtonToggleComponent,
  defineButtonToggleGroupComponent,
  defineCardComponent,
  defineChipComponent,
  defineChipSetComponent,
  defineDividerComponent,
  defineIconButtonComponent,
  defineIconComponent,
  defineInlineMessageComponent,
  defineLabelValueComponent,
  defineOptionComponent,
  defineOptionGroupComponent,
  defineScaffoldComponent,
  defineSelectComponent,
  defineTabBarComponent,
  defineTabComponent,
  defineTableComponent,
  defineTextFieldComponent,
  defineToolbarComponent,
  defineTooltipComponent,
} from '@tylertech/forge';

defineAppBarComponent();
defineBadgeComponent();
defineButtonComponent();
defineButtonToggleComponent();
defineButtonToggleGroupComponent();
defineCardComponent();
defineChipComponent();
defineChipSetComponent();
defineDividerComponent();
defineIconButtonComponent();
defineIconComponent();
defineInlineMessageComponent();
defineLabelValueComponent();
defineOptionComponent();
defineOptionGroupComponent();
defineScaffoldComponent();
defineSelectComponent();
defineTabBarComponent();
defineTabComponent();
defineTableComponent();
defineTextFieldComponent();
defineToolbarComponent();
defineTooltipComponent();
