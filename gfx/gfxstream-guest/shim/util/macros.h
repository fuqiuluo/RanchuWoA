/* RanchuWoA：mesa util/macros.h 的最小替代（仅提供 guest virtgpu 后端用到的宏）。 */
#pragma once

#define DIV_ROUND_UP(value, divisor) (((value) + (divisor)-1) / (divisor))
#define ALIGN_POT(value, pot_align) (((value) + (pot_align)-1) & ~((pot_align)-1))
#define UNUSED __attribute__((unused))
#define ARRAY_SIZE(a) (sizeof(a) / sizeof((a)[0]))
