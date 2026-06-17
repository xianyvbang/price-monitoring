import { computed, onBeforeUnmount, onMounted, ref } from "vue";

const MOBILE_MAX_WIDTH = 760;

export function useViewport() {
  const width = ref(typeof window === "undefined" ? MOBILE_MAX_WIDTH + 1 : window.innerWidth);

  function updateWidth() {
    width.value = window.innerWidth;
  }

  onMounted(() => {
    updateWidth();
    window.addEventListener("resize", updateWidth, { passive: true });
  });

  onBeforeUnmount(() => {
    window.removeEventListener("resize", updateWidth);
  });

  return {
    width,
    isMobile: computed(() => width.value <= MOBILE_MAX_WIDTH)
  };
}
